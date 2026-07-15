from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_trade.brokers.ibkr import _IBApp
from ai_trade.domain import OrderState


async def test_ibkr_execution_waits_for_commission() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue()
    app = _IBApp(asyncio.get_running_loop(), queue)
    app.execution_refs[11] = ("internal-1", "intent-1", "DU123")
    contract = SimpleNamespace(symbol="AAPL")
    execution = SimpleNamespace(
        orderId=11,
        execId="execution-1",
        side="BOT",
        shares=2,
        price=201.25,
    )

    app.execDetails(0, contract, execution)
    assert queue.empty()
    app.commissionReport(SimpleNamespace(execId="execution-1", commission=1.05))

    raw = await queue.get()
    assert isinstance(raw, dict)
    assert str(raw["commission"]) == "1.05"
    assert raw["internal_order_id"] == "internal-1"


async def test_ibkr_submitted_partial_status_is_not_acknowledged() -> None:
    queue: asyncio.Queue[object] = asyncio.Queue()
    app = _IBApp(asyncio.get_running_loop(), queue)
    app.order_refs[11] = ("internal-1", "intent-1", "DU123")

    app.orderStatus(11, "Submitted", 1, 1, 200.0, 1, 0, 200.0, 17, "", 0.0)

    raw = await queue.get()
    assert isinstance(raw, dict)
    assert raw["state"] is OrderState.PARTIALLY_FILLED
