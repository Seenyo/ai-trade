from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ai_trade.domain import MarketEvent
from ai_trade.market import MinuteBarAggregator, build_features, synthetic_bar


def test_minute_aggregator_finishes_only_on_next_bucket(now) -> None:
    aggregator = MinuteBarAggregator()
    first = MarketEvent(
        instrument_id="US:AAPL",
        source="test",
        event_type="BAR_5S",
        event_at=now.replace(second=0),
        received_at=now,
        available_at=now,
        payload={"open": "100", "high": "101", "low": "99", "close": "100.5", "volume": "10"},
    )
    assert aggregator.update(first) is None
    next_minute = first.model_copy(
        update={
            "event_id": first.event_id,
            "event_at": first.event_at + timedelta(minutes=1),
            "received_at": first.received_at + timedelta(minutes=1),
            "available_at": first.available_at + timedelta(minutes=1),
        }
    )
    completed = aggregator.update(next_minute)
    assert completed is not None
    assert completed.volume == Decimal("10")


def test_features_ignore_bars_not_yet_available(now) -> None:
    start = now - timedelta(minutes=40)
    bars = [
        synthetic_bar("US:AAPL", start + timedelta(minutes=i), Decimal(100 + i), Decimal("100"))
        for i in range(35)
    ]
    spy = [
        synthetic_bar("US:SPY", start + timedelta(minutes=i), Decimal("500"), Decimal("100"))
        for i in range(35)
    ]
    qqq = [
        synthetic_bar("US:QQQ", start + timedelta(minutes=i), Decimal("400"), Decimal("100"))
        for i in range(35)
    ]
    future = synthetic_bar("US:AAPL", now + timedelta(minutes=1), Decimal("10000"), Decimal("100"))
    snapshot = build_features("momentum", "US:AAPL", [*bars, future], spy, qqq, now)
    assert snapshot is not None
    assert snapshot.values["last_price"] != 10000


def test_intraday_features_do_not_cross_session_boundary(now) -> None:
    current_start = now - timedelta(minutes=5)
    previous_start = current_start - timedelta(days=1, minutes=30)

    def history(instrument_id: str) -> list:
        previous = [
            synthetic_bar(
                instrument_id,
                previous_start + timedelta(minutes=index),
                Decimal("100"),
                Decimal("100"),
            )
            for index in range(31)
        ]
        current = [
            synthetic_bar(
                instrument_id,
                current_start + timedelta(minutes=index),
                Decimal("101"),
                Decimal("100"),
            )
            for index in range(5)
        ]
        return [*previous, *current]

    assert (
        build_features(
            "momentum",
            "US:AAPL",
            history("US:AAPL"),
            history("US:SPY"),
            history("US:QQQ"),
            now,
        )
        is None
    )
