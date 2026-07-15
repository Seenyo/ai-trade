from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from .config import RiskSettings
from .domain import (
    OrderIntent,
    PortfolioSnapshot,
    PositionEffect,
    RiskDecision,
    Side,
)


class RiskCode:
    SYSTEM_NOT_ARMED = "SYSTEM_NOT_ARMED"
    STALE_DATA = "STALE_DATA"
    EXPIRED_INTENT = "EXPIRED_INTENT"
    DAILY_LOSS = "DAILY_LOSS"
    INSUFFICIENT_SETTLED_CASH = "INSUFFICIENT_SETTLED_CASH"
    POSITION_LIMIT = "POSITION_LIMIT"
    SLEEVE_LIMIT = "SLEEVE_LIMIT"
    GROSS_LIMIT = "GROSS_LIMIT"
    POSITION_COUNT = "POSITION_COUNT"
    SLEEVE_POSITION_COUNT = "SLEEVE_POSITION_COUNT"
    CLOSE_EXCEEDS_POSITION = "CLOSE_EXCEEDS_POSITION"
    PRICE_COLLAR = "PRICE_COLLAR"
    SHORT_PROHIBITED = "SHORT_PROHIBITED"


class RiskEngine:
    def __init__(self, settings: RiskSettings, version: str = "risk-v1") -> None:
        self.settings = settings
        self.version = version

    def evaluate(
        self,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        now: datetime,
        latest_data_at: datetime | None,
        system_armed: bool,
        pending_entries: Sequence[OrderIntent] = (),
    ) -> RiskDecision:
        reasons: list[str] = []
        checks: dict[str, str] = {}

        if not system_armed:
            reasons.append(RiskCode.SYSTEM_NOT_ARMED)
        if intent.expires_at <= now:
            reasons.append(RiskCode.EXPIRED_INTENT)
        if (
            latest_data_at is None
            or (now - latest_data_at).total_seconds() > self.settings.data_stale_seconds
        ):
            reasons.append(RiskCode.STALE_DATA)

        daily_pnl = portfolio.daily_realized_pnl + portfolio.daily_unrealized_pnl
        daily_loss_limit = portfolio.nav * self.settings.daily_loss_fraction
        checks["daily_pnl"] = str(daily_pnl)
        checks["daily_loss_limit"] = str(daily_loss_limit)
        if daily_pnl <= -daily_loss_limit:
            reasons.append(RiskCode.DAILY_LOSS)

        position = next(
            (
                item
                for item in portfolio.positions
                if item.strategy == intent.strategy and item.instrument_id == intent.instrument_id
            ),
            None,
        )
        if intent.effect is PositionEffect.CLOSE:
            if intent.side is not Side.SELL:
                reasons.append(RiskCode.SHORT_PROHIBITED)
            if position is None or intent.quantity > position.quantity:
                reasons.append(RiskCode.CLOSE_EXCEEDS_POSITION)
        else:
            self._evaluate_entry(intent, portfolio, pending_entries, reasons, checks)

        price_move_bps = (
            abs(intent.limit_price - intent.reference_price)
            / intent.reference_price
            * Decimal("10000")
        )
        checks["entry_price_move_bps"] = str(price_move_bps)
        if price_move_bps > self.settings.maximum_entry_slippage_bps:
            reasons.append(RiskCode.PRICE_COLLAR)

        return RiskDecision(
            intent_id=intent.intent_id,
            approved=not reasons,
            reason_codes=tuple(dict.fromkeys(reasons)),
            evaluated_at=now,
            configuration_version=self.version,
            checks=checks,
        )

    def _evaluate_entry(
        self,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        pending_entries: Sequence[OrderIntent],
        reasons: list[str],
        checks: dict[str, str],
    ) -> None:
        if intent.side is not Side.BUY:
            reasons.append(RiskCode.SHORT_PROHIBITED)
            return

        notional = intent.limit_price * Decimal(intent.quantity)
        available_cash = portfolio.settled_cash - portfolio.reserved_cash
        checks["notional"] = str(notional)
        checks["available_settled_cash"] = str(available_cash)
        if notional > available_cash:
            reasons.append(RiskCode.INSUFFICIENT_SETTLED_CASH)

        pending_notionals = tuple(
            (item, item.limit_price * Decimal(item.quantity))
            for item in pending_entries
            if item.effect is PositionEffect.OPEN
        )
        same_position_value = sum(
            (
                item.market_value
                for item in portfolio.positions
                if item.instrument_id == intent.instrument_id
            ),
            Decimal("0"),
        ) + sum(
            (
                value
                for item, value in pending_notionals
                if item.instrument_id == intent.instrument_id
            ),
            Decimal("0"),
        )
        if same_position_value + notional > portfolio.nav * self.settings.maximum_position_fraction:
            reasons.append(RiskCode.POSITION_LIMIT)

        sleeve_value = sum(
            (item.market_value for item in portfolio.positions if item.strategy == intent.strategy),
            Decimal("0"),
        ) + sum(
            (value for item, value in pending_notionals if item.strategy == intent.strategy),
            Decimal("0"),
        )
        if sleeve_value + notional > portfolio.nav * self.settings.maximum_sleeve_fraction:
            reasons.append(RiskCode.SLEEVE_LIMIT)
        if (
            portfolio.gross_exposure
            + sum((value for _item, value in pending_notionals), Decimal("0"))
            + notional
            > portfolio.nav * self.settings.maximum_gross_fraction
        ):
            reasons.append(RiskCode.GROSS_LIMIT)

        symbols = {item.instrument_id for item in portfolio.positions if item.quantity != 0}
        symbols.update(item.instrument_id for item, _value in pending_notionals)
        sleeve_symbols = {
            item.instrument_id
            for item in portfolio.positions
            if item.strategy == intent.strategy and item.quantity != 0
        }
        sleeve_symbols.update(
            item.instrument_id
            for item, _value in pending_notionals
            if item.strategy == intent.strategy
        )
        if intent.instrument_id not in symbols and len(symbols) >= self.settings.maximum_positions:
            reasons.append(RiskCode.POSITION_COUNT)
        if (
            intent.instrument_id not in sleeve_symbols
            and len(sleeve_symbols) >= self.settings.maximum_positions_per_sleeve
        ):
            reasons.append(RiskCode.SLEEVE_POSITION_COUNT)


def size_whole_shares(
    nav: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    settings: RiskSettings,
) -> int:
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        return 0
    risk_budget = nav * settings.risk_per_trade_fraction
    risk_per_share = entry_price - stop_price
    risk_quantity = int(risk_budget / risk_per_share)
    notional_quantity = int((nav * settings.maximum_position_fraction) / entry_price)
    return max(0, min(risk_quantity, notional_quantity))
