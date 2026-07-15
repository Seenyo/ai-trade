# AI Trade

A production-shaped, deterministic US-equity paper-trading platform. Models may propose signals; only the typed risk engine and order-management path can create broker orders.

> **Safety status:** live trading is deliberately disabled. The application refuses `AI_TRADE_MODE=live`.

## V1 behavior

- IBKR paper account through the official TWS API
- 40 liquid US common stocks; SPY/QQQ used only as regime inputs
- Independent momentum and mean-reversion sleeves
- One-minute canonical bars and five-minute decisions
- Long-only, whole-share, settled-cash accounting
- 50% maximum gross exposure, 12.5% per position, four positions total
- Explicit daily arming, automatic kill controls, immutable audit events
- Confirmed bracket cancellation before controlled time exits
- Automatic stale-data, daily-loss, rejection-rate, broker-fault, and position-reconciliation stops
- Conservative next-bar backtesting and purged walk-forward LightGBM training

## Prerequisites

1. Install Python 3.12, [`uv`](https://docs.astral.sh/uv/), and Docker Desktop or OrbStack.
2. Open and fund an IBKR Pro/IBSJ account, create its paper account, and subscribe to the required US market data.
3. Install TWS or IB Gateway. Configure API socket clients and use the paper port (`7497` by default).

## Setup

```bash
cp .env.example .env
# Set the exact paper account ID and replace the operator password in .env.
uv sync --extra dev --extra ibkr
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest
uv run ai-trade-api
```

Open <http://127.0.0.1:8000>. The system will not arm until the configured paper account is connected, flat, and sending fresh subscribed data.

The application currently implements the US-equity first milestone. Domain identifiers, broker ports, sleeve accounting, and research contracts are intentionally venue-neutral so Japanese equities and crypto can be added as separate adapters after the US paper gate passes. They are not enabled in this release.

## Architecture

```text
IBKR callbacks -> raw events -> minute aggregation -> feature snapshots
                                                   -> strategy proposals
                                                   -> deterministic risk
                                                   -> persisted OrderIntent
                                                   -> broker adapter
IBKR fills/status -> OMS + sleeve ledger -> reconciliation/dashboard/audit
```

Canonical operational state is stored in PostgreSQL. Point-in-time research datasets are read from Parquet under `data/`, which is intentionally ignored by Git. Backtest, shadow, paper, and future live adapters share the same domain contracts.

LLMs have no order-submission capability in this architecture. An LLM can later create a typed, expiring research feature or signal proposal; that proposal still passes through the same deterministic sizing, risk, idempotency, OMS, and broker controls.

## Research

Training expects a point-in-time Parquet dataset with an `available_at` column, strategy feature columns, and a binary cost-aware target such as `net_positive_30m`.

```bash
uv run ai-trade-research data/parquet/momentum_training.parquet models/artifacts/momentum.joblib \
  --features return_5m return_15m return_30m relative_spy_15m relative_qqq_15m \
  volume_z realized_vol_30m range_position_30m spread_bps
```

The checked-in universe is frozen as `us-liquid-v1`. Historical results from before its effective date are selection-biased and cannot independently qualify a live rollout.

Replay canonical minute bars with the production feature, strategy, ledger, and risk contracts:

```bash
uv run ai-trade-backtest data/parquet/us_minute_bars.parquet
```

The bar file must contain `instrument_id`, `started_at`, `ended_at`, `available_at`, OHLCV, and may contain bid/ask, completeness, and interval columns. Fills occur on the next bar with adverse stop/target ordering, participation limits, slippage, and commissions.

## Paper evidence gate

Live mode remains disabled even when the evidence gate passes. To evaluate readiness, prepare a CSV or Parquet file with one row per strategy/session and the columns `session_date`, `strategy`, `trade_count`, and `net_return`:

```bash
uv run ai-trade-qualify data/parquet/paper_session_summary.parquet
```

The gate requires at least 90 distinct paper sessions, 500 aggregate trades, sufficient evidence in both sleeves, a positive 95% lower confidence bound on mean daily return, and no sleeve drawdown above 10%. Passing it is evidence for a separate review—not permission to trade live.

## Operational rules

- Never expose the dashboard beyond localhost without TLS and proper identity infrastructure.
- Never store TWS credentials in this project; authenticate interactively in TWS/IB Gateway.
- Use a dedicated paper account with no pre-existing positions.
- A kill cancels working orders and prevents new entries. Confirm broker state manually after any uncertainty.
- Keep the TWS paper-account window and dashboard visible while the system is armed.
- Live eligibility requires at least 90 paper sessions, 500 aggregate trades, strategy-level statistical qualification, and completed fault drills.
