from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from ai_trade.config import RiskSettings
from ai_trade.domain import PortfolioSnapshot
from ai_trade.risk import RiskCode, RiskEngine, size_whole_shares


def empty_portfolio(now: object) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        captured_at=now,
        nav=Decimal("25000"),
        cash=Decimal("25000"),
        settled_cash=Decimal("25000"),
        reserved_cash=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
    )


def test_approves_small_fresh_armed_order(now, buy_intent) -> None:
    decision = RiskEngine(RiskSettings()).evaluate(buy_intent, empty_portfolio(now), now, now, True)
    assert decision.approved


def test_rejects_unarmed_stale_and_oversized(now, buy_intent) -> None:
    oversized = buy_intent.model_copy(update={"quantity": 100})
    decision = RiskEngine(RiskSettings()).evaluate(
        oversized,
        empty_portfolio(now),
        now,
        now - timedelta(seconds=11),
        False,
    )
    assert not decision.approved
    assert RiskCode.SYSTEM_NOT_ARMED in decision.reason_codes
    assert RiskCode.STALE_DATA in decision.reason_codes
    assert RiskCode.POSITION_LIMIT in decision.reason_codes


def test_pending_entries_count_toward_sleeve_limits(now, buy_intent) -> None:
    first = buy_intent.model_copy(
        update={"instrument_id": "US:MSFT", "idempotency_key": "pending-msft"}
    )
    second = buy_intent.model_copy(
        update={"instrument_id": "US:NVDA", "idempotency_key": "pending-nvda"}
    )
    candidate = buy_intent.model_copy(
        update={"instrument_id": "US:AMZN", "idempotency_key": "candidate-amzn"}
    )
    decision = RiskEngine(RiskSettings()).evaluate(
        candidate,
        empty_portfolio(now),
        now,
        now,
        True,
        (first, second),
    )
    assert not decision.approved
    assert RiskCode.SLEEVE_POSITION_COUNT in decision.reason_codes


@given(
    nav=st.decimals(min_value="1000", max_value="1000000", places=2),
    entry=st.decimals(min_value="10", max_value="1000", places=2),
    stop_fraction=st.decimals(min_value="0.001", max_value="0.05", places=4),
)
def test_sizing_never_exceeds_position_limit(nav, entry, stop_fraction) -> None:
    settings = RiskSettings()
    stop = entry * (Decimal("1") - stop_fraction)
    quantity = size_whole_shares(nav, entry, stop, settings)
    assert Decimal(quantity) * entry <= nav * settings.maximum_position_fraction
