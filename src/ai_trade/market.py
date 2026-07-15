from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from .domain import Bar, FeatureSnapshot, MarketEvent

ET = ZoneInfo("America/New_York")
MAX_FEATURE_BAR_AGE = timedelta(minutes=1)


@dataclass
class _MinuteState:
    bucket: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class MinuteBarAggregator:
    """Aggregates IBKR five-second bars and quote ticks into canonical minute bars."""

    def __init__(self) -> None:
        self._current: dict[str, _MinuteState] = {}
        self._quotes: dict[str, tuple[Decimal | None, Decimal | None]] = {}

    def update(self, event: MarketEvent) -> Bar | None:
        if event.event_type == "PRICE":
            tick_type = int(event.payload["tick_type"])
            bid, ask = self._quotes.get(event.instrument_id, (None, None))
            if tick_type == 1:
                bid = Decimal(str(event.payload["price"]))
            elif tick_type == 2:
                ask = Decimal(str(event.payload["price"]))
            self._quotes[event.instrument_id] = (bid, ask)
            return None
        if event.event_type != "BAR_5S":
            return None

        bucket = event.event_at.replace(second=0, microsecond=0)
        state = self._current.get(event.instrument_id)
        completed = None
        if state is not None and state.bucket != bucket:
            completed = self._finish(event.instrument_id, state, event.received_at)
            state = None
        payload = event.payload
        if state is None:
            state = _MinuteState(
                bucket=bucket,
                open=Decimal(str(payload["open"])),
                high=Decimal(str(payload["high"])),
                low=Decimal(str(payload["low"])),
                close=Decimal(str(payload["close"])),
                volume=Decimal(str(payload["volume"])),
            )
            self._current[event.instrument_id] = state
        else:
            state.high = max(state.high, Decimal(str(payload["high"])))
            state.low = min(state.low, Decimal(str(payload["low"])))
            state.close = Decimal(str(payload["close"]))
            state.volume += Decimal(str(payload["volume"]))
        return completed

    def _finish(self, instrument_id: str, state: _MinuteState, available_at: datetime) -> Bar:
        bid, ask = self._quotes.get(instrument_id, (None, None))
        return Bar(
            instrument_id=instrument_id,
            started_at=state.bucket,
            ended_at=state.bucket + timedelta(minutes=1),
            available_at=available_at,
            open=state.open,
            high=state.high,
            low=state.low,
            close=state.close,
            volume=state.volume,
            bid=bid,
            ask=ask,
        )


class BarStore:
    def __init__(self, maximum_bars: int = 500) -> None:
        self._bars: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=maximum_bars))

    def append(self, bar: Bar) -> None:
        existing = self._bars[bar.instrument_id]
        if existing and bar.started_at <= existing[-1].started_at:
            raise ValueError("bars must be appended in chronological order")
        existing.append(bar)

    def get(self, instrument_id: str, count: int | None = None) -> tuple[Bar, ...]:
        bars = self._bars[instrument_id]
        if count is None:
            return tuple(bars)
        return tuple(list(bars)[-count:])

    def latest_at(self, instrument_id: str) -> datetime | None:
        bars = self._bars[instrument_id]
        return bars[-1].available_at if bars else None


def _return(closes: Sequence[float], periods: int) -> float:
    if len(closes) <= periods or closes[-periods - 1] == 0:
        return 0.0
    return closes[-1] / closes[-periods - 1] - 1.0


def build_features(
    strategy: str,
    instrument_id: str,
    bars: Sequence[Bar],
    spy_bars: Sequence[Bar],
    qqq_bars: Sequence[Bar],
    decision_at: datetime,
) -> FeatureSnapshot | None:
    if len(bars) < 31 or len(spy_bars) < 31 or len(qqq_bars) < 31:
        return None
    trading_day = decision_at.astimezone(ET).date()

    def eligible_bars(values: Sequence[Bar]) -> list[Bar]:
        return sorted(
            (
                bar
                for bar in values
                if bar.available_at <= decision_at
                and bar.complete
                and bar.ended_at.astimezone(ET).date() == trading_day
            ),
            key=lambda bar: bar.ended_at,
        )

    eligible = eligible_bars(bars)
    spy = eligible_bars(spy_bars)
    qqq = eligible_bars(qqq_bars)
    if len(eligible) < 31 or len(spy) < 31 or len(qqq) < 31:
        return None
    if any(
        decision_at - series[-1].ended_at > MAX_FEATURE_BAR_AGE for series in (eligible, spy, qqq)
    ):
        return None

    closes = [float(bar.close) for bar in eligible]
    volumes = [float(bar.volume) for bar in eligible]
    spy_closes = [float(bar.close) for bar in spy]
    qqq_closes = [float(bar.close) for bar in qqq]
    typical_dollars = [
        float((bar.high + bar.low + bar.close) / Decimal(3) * bar.volume) for bar in eligible
    ]
    total_volume = sum(volumes)
    vwap = (
        sum(
            float((bar.high + bar.low + bar.close) / Decimal(3)) * float(bar.volume)
            for bar in eligible
        )
        / total_volume
        if total_volume
        else closes[-1]
    )
    one_minute_returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(max(1, len(closes) - 30), len(closes))
        if closes[index - 1]
    ]
    volume_window = volumes[-21:-1]
    volume_std = pstdev(volume_window) if len(volume_window) > 1 else 0.0
    volume_z = (volumes[-1] - fmean(volume_window)) / volume_std if volume_std else 0.0
    spread_bps = 0.0
    if eligible[-1].bid is not None and eligible[-1].ask is not None and closes[-1]:
        spread_bps = float((eligible[-1].ask - eligible[-1].bid) / eligible[-1].close * 10000)
    window_high = max(float(bar.high) for bar in eligible[-30:])
    window_low = min(float(bar.low) for bar in eligible[-30:])
    range_position = (
        (closes[-1] - window_low) / (window_high - window_low) if window_high > window_low else 0.5
    )
    notional_liquidity = fmean(typical_dollars[-20:]) if typical_dollars else 0.0
    values = {
        "return_5m": _return(closes, 5),
        "return_15m": _return(closes, 15),
        "return_30m": _return(closes, 30),
        "spy_return_15m": _return(spy_closes, 15),
        "qqq_return_15m": _return(qqq_closes, 15),
        "relative_spy_15m": _return(closes, 15) - _return(spy_closes, 15),
        "relative_qqq_15m": _return(closes, 15) - _return(qqq_closes, 15),
        "realized_vol_30m": pstdev(one_minute_returns) if len(one_minute_returns) > 1 else 0.0,
        "volume_z": volume_z,
        "vwap_distance": closes[-1] / vwap - 1.0 if vwap else 0.0,
        "range_position_30m": range_position,
        "spread_bps": spread_bps,
        "dollar_volume_20m": notional_liquidity,
        "last_price": closes[-1],
    }
    return FeatureSnapshot(
        strategy=strategy,
        instrument_id=instrument_id,
        decision_at=decision_at.astimezone(UTC),
        feature_set_version="intraday-v1",
        values=values,
    )


def synthetic_bar(instrument_id: str, started_at: datetime, price: Decimal, volume: Decimal) -> Bar:
    """Convenience fixture used by smoke tests and examples."""
    ended_at = started_at + timedelta(minutes=1)
    return Bar(
        instrument_id=instrument_id,
        started_at=started_at,
        ended_at=ended_at,
        available_at=ended_at,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        bid=price - Decimal("0.01"),
        ask=price + Decimal("0.01"),
    )
