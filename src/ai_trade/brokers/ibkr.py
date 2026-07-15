from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..config import BrokerSettings
from ..domain import (
    BrokerFault,
    BrokerOrder,
    Execution,
    MarketEvent,
    OrderIntent,
    OrderState,
    PortfolioSnapshot,
    PositionLot,
    Side,
)

try:
    from ibapi.client import EClient
    from ibapi.commission_report import CommissionReport
    from ibapi.contract import Contract
    from ibapi.execution import Execution as IBExecution
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper
except ImportError:  # pragma: no cover - exercised only without optional dependency
    EClient = object
    EWrapper = object
    Contract = Any
    Order = Any
    IBExecution = Any
    CommissionReport = Any


_NASDAQ_SYMBOLS = {
    "AAPL",
    "AMD",
    "AMGN",
    "AMZN",
    "AVGO",
    "COST",
    "CSCO",
    "GOOG",
    "GOOGL",
    "INTC",
    "META",
    "MSFT",
    "NFLX",
    "NVDA",
    "PEP",
    "QCOM",
    "TSLA",
}
_PRIMARY_EXCHANGE = {symbol: "NASDAQ" for symbol in _NASDAQ_SYMBOLS}
_PRIMARY_EXCHANGE.update({"QQQ": "NASDAQ", "SPY": "ARCA"})


class _IBApp(EWrapper, EClient):  # type: ignore[misc]
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Any]) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.loop = loop
        self.queue = queue
        self.ready = threading.Event()
        self.accounts: set[str] = set()
        self.next_order_id: int | None = None
        self.request_symbols: dict[int, str] = {}
        self.order_refs: dict[int, tuple[str, str, str]] = {}
        self.execution_refs: dict[int, tuple[str, str, str]] = {}
        self.pending_executions: dict[str, dict[str, Any]] = {}
        self.pending_commissions: dict[str, Decimal] = {}
        self.cancellation_waiters: dict[int, asyncio.Event] = {}
        self.account_values: dict[str, Decimal] = {}
        self.positions: dict[str, tuple[int, Decimal, Decimal]] = {}
        self.account_download_complete = threading.Event()

    def _emit(self, value: Any) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, value)

    def nextValidId(self, orderId: int) -> None:
        self.next_order_id = orderId
        self.ready.set()

    def managedAccounts(self, accountsList: str) -> None:
        self.accounts = {value.strip() for value in accountsList.split(",") if value.strip()}

    def updateAccountValue(self, key: str, val: str, currency: str, accountName: str) -> None:
        del accountName
        if currency in {"USD", "BASE"}:
            try:
                self.account_values[key] = Decimal(val)
            except Exception:
                return

    def position(self, account: str, contract: Contract, position: Decimal, avgCost: float) -> None:
        del account
        instrument_id = f"US:{contract.symbol}"
        if contract.secType == "STK" and position:
            self.positions[instrument_id] = (
                int(position),
                Decimal(str(avgCost)),
                Decimal(str(avgCost)),
            )
        elif contract.secType == "STK":
            self.positions.pop(instrument_id, None)

    def positionEnd(self) -> None:
        self.account_download_complete.set()

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        del attrib
        symbol = self.request_symbols.get(reqId)
        if symbol is None or price <= 0:
            return
        now = datetime.now(UTC)
        self._emit(
            MarketEvent(
                instrument_id=f"US:{symbol}",
                source="IBKR",
                event_type="PRICE",
                event_at=now,
                received_at=now,
                available_at=now,
                payload={"tick_type": tickType, "price": price},
            )
        )

    def realtimeBar(
        self,
        reqId: int,
        time: int,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: Decimal,
        wap: Decimal,
        count: int,
    ) -> None:
        del wap, count
        symbol = self.request_symbols.get(reqId)
        if symbol is None:
            return
        received = datetime.now(UTC)
        event_at = datetime.fromtimestamp(time, tz=UTC)
        self._emit(
            MarketEvent(
                instrument_id=f"US:{symbol}",
                source="IBKR",
                event_type="BAR_5S",
                event_at=event_at,
                received_at=received,
                available_at=received,
                payload={
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": str(volume),
                },
            )
        )

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        del remaining, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice
        if status in {"Cancelled", "ApiCancelled", "Inactive"}:
            waiter = self.cancellation_waiters.get(orderId)
            if waiter is not None:
                self.loop.call_soon_threadsafe(waiter.set)
        reference = self.order_refs.get(orderId)
        if reference is None:
            return
        internal_id, intent_id, account_id = reference
        state_map = {
            "PendingSubmit": OrderState.SUBMITTING,
            "PreSubmitted": OrderState.ACKNOWLEDGED,
            "Submitted": OrderState.ACKNOWLEDGED,
            "PendingCancel": OrderState.CANCEL_PENDING,
            "Cancelled": OrderState.CANCELLED,
            "ApiCancelled": OrderState.CANCELLED,
            "Filled": OrderState.FILLED,
            "Inactive": OrderState.REJECTED,
        }
        state = state_map.get(status, OrderState.RECONCILIATION_REQUIRED)
        if filled > 0 and state is OrderState.ACKNOWLEDGED:
            state = OrderState.PARTIALLY_FILLED
        now = datetime.now(UTC)
        self._emit(
            {
                "type": "order_status",
                "internal_order_id": internal_id,
                "intent_id": intent_id,
                "account_id": account_id,
                "broker_order_id": str(orderId),
                "state": state,
                "filled": int(filled),
                "average_fill_price": Decimal(str(avgFillPrice)) if avgFillPrice else None,
                "at": now,
            }
        )

    def execDetails(self, reqId: int, contract: Contract, execution: IBExecution) -> None:
        del reqId
        reference = self.execution_refs.get(execution.orderId)
        if reference is None:
            return
        internal_id, _, _ = reference
        pending = {
            "type": "execution",
            "internal_order_id": internal_id,
            "broker_execution_id": execution.execId,
            "instrument_id": f"US:{contract.symbol}",
            "side": Side.BUY if execution.side in {"BOT", "BUY"} else Side.SELL,
            "quantity": int(execution.shares),
            "price": Decimal(str(execution.price)),
            "at": datetime.now(UTC),
        }
        commission = self.pending_commissions.pop(execution.execId, None)
        if commission is not None:
            pending["commission"] = commission
            self._emit(pending)
        else:
            self.pending_executions[execution.execId] = pending

    def commissionReport(self, commissionReport: CommissionReport) -> None:
        pending = self.pending_executions.pop(commissionReport.execId, None)
        if pending is None:
            self.pending_commissions[commissionReport.execId] = Decimal(
                str(commissionReport.commission)
            )
            return
        pending["commission"] = Decimal(str(commissionReport.commission))
        self._emit(pending)

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        reference = self.order_refs.get(reqId)
        if reference is not None:
            internal_id, intent_id, account_id = reference
            message = errorString
            if advancedOrderRejectJson:
                message = f"{message}: {advancedOrderRejectJson}"
            self._emit(
                {
                    "type": "order_status",
                    "internal_order_id": internal_id,
                    "intent_id": intent_id,
                    "account_id": account_id,
                    "broker_order_id": str(reqId),
                    "state": OrderState.REJECTED,
                    "filled": 0,
                    "average_fill_price": None,
                    "at": datetime.now(UTC),
                    "error": message,
                }
            )
            return
        informational = {2104, 2106, 2107, 2108, 2158}
        self._emit(
            BrokerFault(
                code=errorCode,
                message=errorString,
                request_id=reqId,
                fatal=errorCode not in informational,
                occurred_at=datetime.now(UTC),
            )
        )

    def connectionClosed(self) -> None:
        self._emit(
            BrokerFault(
                code=0,
                message="IBKR socket connection closed",
                fatal=True,
                occurred_at=datetime.now(UTC),
            )
        )


class IBKRBroker:
    """Official TWS API boundary. This release refuses live-mode configuration upstream."""

    def __init__(self, settings: BrokerSettings) -> None:
        if EClient is object:
            raise RuntimeError("Install the optional ibkr dependency to use IBKRBroker")
        self.settings = settings
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._app: _IBApp | None = None
        self._thread: threading.Thread | None = None
        self._internal_orders: dict[str, BrokerOrder] = {}
        self._intent_strategy: dict[str, str] = {}
        self._bracket_order_ids: dict[str, tuple[int, ...]] = {}

    @property
    def account_id(self) -> str:
        return self.settings.account_id

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._app = _IBApp(loop, self._queue)
        self._app.connect(self.settings.host, self.settings.port, self.settings.client_id)
        self._thread = threading.Thread(target=self._app.run, name="ibkr-api", daemon=True)
        self._thread.start()
        ready = await asyncio.to_thread(self._app.ready.wait, self.settings.connect_timeout_seconds)
        if not ready:
            self._app.disconnect()
            raise TimeoutError("IBKR did not provide a valid order ID")

    async def disconnect(self) -> None:
        if self._app is not None:
            self._app.disconnect()

    async def verify_paper_account(self, expected_account_id: str) -> None:
        if self._app is None:
            raise RuntimeError("IBKR is disconnected")
        if expected_account_id != self.settings.account_id:
            raise RuntimeError("configured and expected account IDs differ")
        for _ in range(30):
            if expected_account_id in self._app.accounts:
                self._app.reqAccountUpdates(True, expected_account_id)
                self._app.reqPositions()
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("configured paper account is not managed by this TWS session")

    async def events(
        self,
    ) -> AsyncIterator[MarketEvent | Execution | BrokerOrder | BrokerFault]:
        while self._app is not None and self._app.isConnected():
            raw = await self._queue.get()
            if isinstance(raw, (MarketEvent, Execution, BrokerOrder, BrokerFault)):
                yield raw
            elif raw.get("type") == "order_status":
                order = self._internal_orders[raw["internal_order_id"]]
                updated = order.model_copy(
                    update={
                        "broker_order_id": raw["broker_order_id"],
                        "state": raw["state"],
                        "filled_quantity": raw["filled"],
                        "average_fill_price": raw["average_fill_price"],
                        "updated_at": raw["at"],
                    }
                )
                self._internal_orders[raw["internal_order_id"]] = updated
                yield updated
            elif raw.get("type") == "execution":
                order = self._internal_orders[raw["internal_order_id"]]
                yield Execution(
                    broker_execution_id=raw["broker_execution_id"],
                    internal_order_id=order.internal_order_id,
                    strategy=self._intent_strategy[str(order.intent_id)],
                    instrument_id=raw["instrument_id"],
                    side=raw["side"],
                    quantity=raw["quantity"],
                    price=raw["price"],
                    commission=raw["commission"],
                    executed_at=raw["at"],
                )

    async def subscribe(self, instrument_ids: Sequence[str]) -> None:
        app = self._require_app()
        for offset, instrument_id in enumerate(instrument_ids):
            request_id = 1000 + offset
            bar_request_id = 5000 + offset
            symbol = instrument_id.split(":")[-1]
            contract = self._contract(symbol)
            app.request_symbols[request_id] = symbol
            app.request_symbols[bar_request_id] = symbol
            app.reqMktData(request_id, contract, "", False, False, [])
            app.reqRealTimeBars(bar_request_id, contract, 5, "TRADES", True, [])

    async def submit(self, intent: OrderIntent) -> BrokerOrder:
        app = self._require_app()
        if app.next_order_id is None:
            raise RuntimeError("IBKR order ID is unavailable")
        parent_id = app.next_order_id
        app.next_order_id += 3
        now = datetime.now(UTC)
        record = BrokerOrder(
            intent_id=intent.intent_id,
            broker_account_id=self.settings.account_id,
            state=OrderState.SUBMITTING,
            submitted_quantity=intent.quantity,
            created_at=now,
            updated_at=now,
        )
        internal_id = str(record.internal_order_id)
        self._internal_orders[internal_id] = record
        self._intent_strategy[str(intent.intent_id)] = intent.strategy
        contract = self._contract(intent.instrument_id.split(":")[-1])
        parent = self._limit_order(intent, parent_id, transmit=intent.stop_price is None)
        app.order_refs[parent_id] = (internal_id, str(intent.intent_id), self.settings.account_id)
        app.execution_refs[parent_id] = app.order_refs[parent_id]
        bracket_ids: tuple[int, ...] = (parent_id,)
        app.placeOrder(parent_id, contract, parent)
        if intent.stop_price is not None:
            take_profit = self._take_profit(intent, parent_id + 1, parent_id)
            stop = self._stop(intent, parent_id + 2, parent_id)
            app.execution_refs[parent_id + 1] = app.order_refs[parent_id]
            app.execution_refs[parent_id + 2] = app.order_refs[parent_id]
            bracket_ids = (parent_id, parent_id + 1, parent_id + 2)
            app.placeOrder(parent_id + 1, contract, take_profit)
            app.placeOrder(parent_id + 2, contract, stop)
        self._bracket_order_ids[internal_id] = bracket_ids
        return record

    async def cancel(self, internal_order_id: str) -> None:
        app = self._require_app()
        order_ids = self._bracket_order_ids.get(internal_order_id)
        if order_ids is None:
            raise RuntimeError("unknown internal order")
        order = self._internal_orders[internal_order_id]
        target_ids = order_ids[1:] if order.state is OrderState.FILLED else order_ids
        waiters = {order_id: asyncio.Event() for order_id in target_ids}
        app.cancellation_waiters.update(waiters)
        for order_id in target_ids:
            app.cancelOrder(order_id, "")
        try:
            await asyncio.wait_for(
                asyncio.gather(*(waiter.wait() for waiter in waiters.values())),
                timeout=5,
            )
        finally:
            for order_id in target_ids:
                app.cancellation_waiters.pop(order_id, None)

    async def cancel_all(self) -> None:
        self._require_app().reqGlobalCancel()

    async def account_snapshot(self) -> PortfolioSnapshot:
        app = self._require_app()
        await asyncio.to_thread(app.account_download_complete.wait, 5)
        now = datetime.now(UTC)
        nav = app.account_values.get("NetLiquidation", Decimal("0"))
        cash = app.account_values.get("TotalCashValue", Decimal("0"))
        settled = app.account_values.get("SettledCash", cash)
        positions = tuple(
            PositionLot(
                strategy="broker",
                instrument_id=instrument_id,
                quantity=quantity,
                average_price=average_price,
                market_price=market_price,
            )
            for instrument_id, (quantity, average_price, market_price) in app.positions.items()
        )
        return PortfolioSnapshot(
            captured_at=now,
            nav=nav,
            cash=cash,
            settled_cash=settled,
            reserved_cash=Decimal("0"),
            daily_realized_pnl=Decimal("0"),
            daily_unrealized_pnl=Decimal("0"),
            positions=positions,
        )

    def _require_app(self) -> _IBApp:
        if self._app is None or not self._app.isConnected():
            raise RuntimeError("IBKR is disconnected")
        return self._app

    @staticmethod
    def _contract(symbol: str) -> Contract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.primaryExchange = _PRIMARY_EXCHANGE.get(symbol, "NYSE")
        contract.currency = "USD"
        return contract

    def _limit_order(self, intent: OrderIntent, order_id: int, transmit: bool) -> Order:
        order = Order()
        order.orderId = order_id
        order.account = self.settings.account_id
        order.action = intent.side.value
        order.orderType = "LMT"
        order.totalQuantity = intent.quantity
        order.lmtPrice = float(intent.limit_price)
        order.tif = "DAY"
        order.transmit = transmit
        order.orderRef = str(intent.intent_id)
        return order

    def _take_profit(self, intent: OrderIntent, order_id: int, parent_id: int) -> Order:
        order = Order()
        order.orderId = order_id
        order.parentId = parent_id
        order.account = self.settings.account_id
        order.action = "SELL"
        order.orderType = "LMT"
        order.totalQuantity = intent.quantity
        order.lmtPrice = float(intent.target_price or intent.limit_price)
        order.tif = "DAY"
        order.transmit = False
        order.orderRef = str(intent.intent_id)
        return order

    def _stop(self, intent: OrderIntent, order_id: int, parent_id: int) -> Order:
        if intent.stop_price is None:
            raise ValueError("stop order requires stop_price")
        order = Order()
        order.orderId = order_id
        order.parentId = parent_id
        order.account = self.settings.account_id
        order.action = "SELL"
        order.orderType = "STP"
        order.totalQuantity = intent.quantity
        order.auxPrice = float(intent.stop_price)
        order.tif = "DAY"
        order.transmit = True
        order.orderRef = str(intent.intent_id)
        return order
