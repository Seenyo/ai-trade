# AI Trade: Project Overview, Scope, and Progress

**Document status:** Current as of July 16, 2026

**Current milestone:** US-equity paper trading

**Current release:** `0.1.0`

**Safety status:** Live trading is disabled in code

## 1. What the project is about

AI Trade is a production-shaped, supervised day-trading platform for researching strategies and executing them through a broker paper account. The first milestone focuses on liquid US common stocks through the official Interactive Brokers TWS API.

The project separates statistical or machine-learning decisions from order execution. A strategy may analyze market data and propose a typed, short-lived signal, but it cannot contact the broker directly. Every proposed trade must pass deterministic position sizing, portfolio risk checks, order-management rules, persistence, and broker-account validation before an order can be submitted.

This separation is central to the design:

```text
Market data -> canonical bars -> features -> strategy/ML proposal
                                              |
                                              v
Broker API <- order adapter <- OMS <- deterministic risk controls
     |
     v
Executions -> sleeve ledger -> reconciliation -> audit/dashboard
```

### Role of AI and machine learning

The system is designed to use data-driven models as signal scorers, not as autonomous trading agents.

- Baseline momentum and mean-reversion logic determines whether market conditions qualify for a possible trade.
- An optional LightGBM model can score the probability or quality of a qualifying setup.
- A future LLM integration may produce research annotations, structured features, or typed signal proposals.
- An LLM will not receive broker credentials or direct order-submission capability.
- Model output never bypasses the deterministic risk engine, order-management system, or supervised arming process.

The objective is therefore not to let an AI improvise trades. It is to build a measurable trading system in which models contribute evidence and conventional software controls all financial actions.

### Main project goals

1. Research intraday strategies without look-ahead bias.
2. Replay those strategies with conservative execution assumptions.
3. Run the same domain, feature, risk, and accounting logic against an IBKR paper account.
4. Record enough operational and statistical evidence to decide whether further development is justified.
5. Preserve an architecture that can later support Japanese equities, other foreign equities, and crypto through separate venue adapters.

## 2. Project scope

### 2.1 Current V1 trading scope

The implemented V1 milestone has the following boundaries.

| Area | V1 scope |
| --- | --- |
| Venue | Interactive Brokers paper account through TWS or IB Gateway |
| Instruments | 40 frozen, liquid US common stocks |
| Regime inputs | SPY and QQQ; indicators only, not trading candidates |
| Direction | Long-only |
| Position format | Whole shares only |
| Account model | Cash and settled-cash accounting; no margin assumption |
| Starting paper NAV | USD 25,000 |
| Market-data frequency | IBKR ticks and five-second bars aggregated into one-minute bars |
| Decision frequency | Every five minutes during configured entry windows |
| Holding period | Intraday, with a maximum target holding period of 60 minutes |
| Execution | Limit entries with attached take-profit and stop orders |
| Supervision | Explicit daily operator arming and a localhost dashboard |
| Operating-cost target | Prefer local and existing broker infrastructure, targeting no more than approximately JPY 10,000 per month before brokerage and exchange-specific fees |

The universe is stored as `us-liquid-v1` in `config/universe.json`. It is frozen with an effective date so results cannot silently benefit from changing the instrument list after observing performance. Backtests covering dates before that effective date are subject to selection bias and cannot independently qualify the system for live use.

### 2.2 Strategy scope

V1 contains two independent strategy sleeves with separate position limits.

#### Momentum sleeve

The momentum strategy looks for intraday continuation using inputs such as:

- five-, fifteen-, and thirty-minute returns;
- relative performance against SPY and QQQ;
- volume z-score;
- thirty-minute range position;
- realized volatility;
- spread and estimated trading costs.

Its configured entry window is 09:45 through 14:45 US Eastern Time.

#### Mean-reversion sleeve

The mean-reversion strategy looks for liquid intraday pullbacks that may revert toward VWAP. Inputs include:

- short-horizon negative returns;
- distance below intraday VWAP;
- position within the recent price range;
- volume behavior;
- market-regime filters;
- spread and estimated trading costs.

Its configured entry window is 10:00 through 15:00 US Eastern Time.

The two sleeves share capital controls but retain separate position ownership and P&L accounting. Each sleeve is capped at 25% of NAV and two simultaneous positions under the default configuration.

### 2.3 Market-data and feature scope

The market pipeline:

1. receives IBKR quote ticks and five-second real-time bars;
2. converts them into canonical immutable market events;
3. aggregates complete one-minute OHLCV bars;
4. retains bid and ask information when available;
5. builds point-in-time feature snapshots using only data available at the decision time;
6. prevents features from crossing US trading-session boundaries;
7. requires the candidate stock, SPY, and QQQ inputs to each be fresh within one minute;
8. collects decision-minute bars behind an all-subscription barrier with a bounded timeout;
9. requires exact decision-minute alignment for SPY, QQQ, and every candidate evaluated after the barrier.

Fresh events from one subscription cannot make another stale instrument eligible for a signal. Feature generation returns no proposal when the instrument or either regime series is stale, incomplete, unavailable, or lacks sufficient same-session history.

### 2.4 Risk-management scope

Risk controls are deterministic and independent of model confidence. Default limits include:

- 50% maximum gross exposure;
- 12.5% maximum exposure per instrument;
- 25% maximum exposure per strategy sleeve;
- four positions across the account;
- two positions per sleeve;
- 0.15% of NAV risk budget per trade;
- 0.75% daily loss limit;
- ten-basis-point entry-price collar;
- settled-cash validation;
- no short selling;
- no closing quantity greater than the sleeve-owned position;
- approved-but-unfilled entry orders included in portfolio and sleeve limits;
- duplicate intent prevention through idempotency keys.

The engine will kill or refuse trading for conditions including:

- stale global market data;
- stale per-instrument or regime feature data;
- paper-account mismatch;
- unexpected positions before initial arming;
- persistent broker-versus-ledger position mismatch;
- repeated broker order rejections;
- daily loss-limit breach;
- broker connectivity or fatal callback faults;
- uncertain order submission or bracket cancellation;
- persistence failures after broker submission;
- decision-cycle or event-processing failures.

A kill prevents new orders and requests cancellation of working orders. Because cancellation and liquidation are not equivalent, the operator must verify broker state manually after uncertainty or a kill event.

### 2.5 Order and execution scope

The order-management path supports:

- immutable order intents;
- strict order-state transitions;
- limit entry orders;
- attached take-profit and stop children;
- separate parent-order status and child-execution handling;
- broker execution deduplication;
- commission attachment and accounting;
- partial-fill tracking;
- reservation and release of cash for pending entries;
- confirmed bracket-child cancellation before controlled timed exits;
- maximum-holding and end-of-session exits;
- rejection-rate monitoring;
- broker error and disconnection events.

The production adapter uses the official IBKR Python API. A fake broker implements the same boundary for deterministic tests.

### 2.6 Portfolio and accounting scope

The internal ledger is:

- long-only;
- whole-share based;
- strategy-sleeve aware;
- commission aware;
- settled-cash aware;
- execution-idempotent;
- marked to the latest canonical price.

It maintains cash, settled cash, reserved cash, realized P&L, unrealized P&L, NAV, gross exposure, and per-sleeve positions. Sale proceeds remain unsettled until an explicit settlement operation.

### 2.7 Research and backtesting scope

The research pipeline reads point-in-time Parquet datasets, performs purged walk-forward splits with an embargo, trains LightGBM classifiers, and records out-of-sample ROC AUC and Brier score before fitting a final artifact.

The backtester reuses production feature, strategy, sizing, risk, and ledger contracts. Its conservative assumptions include:

- signal decisions use only causally available data;
- orders cannot fill on the signal bar;
- next-bar limit fills;
- adverse ordering when a stop and target occur within the same bar;
- volume participation limits and possible partial fills;
- entry and exit slippage;
- commissions;
- timed and end-of-dataset closures;
- each forced closure uses the latest causally available same-session bar for that instrument;
- the replay fails rather than silently retain a position when no valid forced-exit price exists.

Backtest output includes ending NAV, trades, net P&L, exit reasons, maximum drawdown, and annualized Sharpe ratio.

### 2.8 Paper-trading evidence gate

Passing a paper evidence gate does not enable live mode. It only provides evidence for a separate engineering, operational, legal, and financial review.

The implemented qualification command expects session-level strategy summaries and requires:

- at least 90 distinct paper sessions;
- at least 500 aggregate paper trades;
- sufficient observations and trades in both strategy sleeves;
- a positive 95% lower confidence bound on average daily return for each sleeve;
- maximum drawdown no greater than 10% per sleeve.

Fault drills, broker reconciliation checks, and supervised operating experience are also required before considering a later live-trading project.

### 2.9 Persistence and audit scope

PostgreSQL is the canonical operational store. The initial Alembic migration defines:

- market events;
- canonical bars;
- order intents;
- risk decisions;
- broker orders;
- executions;
- append-oriented audit records;
- an outbox table for future integrations.

Research and replay datasets use Parquet. Secrets and broker login credentials are not stored in the repository. TWS or IB Gateway authentication remains interactive and external to the application.

### 2.10 Operator interface scope

The FastAPI dashboard is intended for localhost use by one supervised operator. It provides:

- password-based local session authentication;
- CSRF protection for operator commands;
- system status and current reason;
- NAV, cash, reserved cash, and daily P&L;
- positions and order states;
- health and audit endpoints;
- server-sent event updates;
- arm, disarm, cancel-all, kill, and kill-acknowledgement controls.

The dashboard must not be exposed publicly without TLS, durable identity management, authorization controls, and additional security review.

### 2.11 Explicitly out of scope for V1

The following are not currently implemented or authorized:

- live-money order submission;
- Japanese-equity trading;
- crypto trading;
- non-US foreign-equity adapters;
- short selling, leverage, or margin strategies;
- fractional shares;
- options, futures, forex, or derivatives;
- market-making or sub-minute high-frequency strategies;
- direct LLM access to a broker;
- fully unattended operation;
- public or multi-user dashboard deployment;
- tax-lot optimization or jurisdiction-specific tax reporting;
- production disaster recovery, high availability, or cloud deployment.

Japanese equities and crypto remain planned venue expansions. They should receive independent market-data, calendar, instrument, fee, settlement, order, and regulatory adapters rather than being added as special cases to the US implementation.

## 3. Current progress

### 3.1 Implementation status

| Workstream | Status | Current state |
| --- | --- | --- |
| Repository and Python tooling | Complete | Python 3.12 package, `uv` lockfile, CLI entry points, Ruff, mypy, and pytest configuration |
| Domain model | Complete for V1 | Immutable typed events, bars, signals, intents, orders, executions, positions, portfolios, commands, and faults |
| US universe | Complete for V1 | Frozen 40-stock universe plus SPY and QQQ regime inputs |
| Market pipeline | Complete in code | IBKR events, one-minute aggregation, point-in-time features, same-session and per-series freshness controls |
| Strategy baselines | Complete in code | Momentum and mean-reversion rules with optional model scoring |
| Risk engine | Complete for defined V1 limits | Cash, exposure, count, sleeve, loss, freshness, price-collar, and close-quantity checks |
| OMS and ledger | Complete for paper milestone | Strict transitions, idempotency, partial fills, commissions, reservations, sleeve positions, and P&L |
| IBKR adapter | Implemented; real-session validation pending | Paper-account verification, subscriptions, bracket orders, callbacks, faults, commissions, cancellation confirmation, and snapshots |
| Backtester | Complete for current bar model | Conservative replay with causal sparse-data forced exits |
| ML research CLI | Complete in code | Purged walk-forward LightGBM training and metric output |
| Qualification CLI | Complete in code | 90-session/500-trade and sleeve-level statistical gate |
| PostgreSQL persistence | Implemented; service validation pending | SQLAlchemy repository, schema creation, and initial Alembic migration |
| Operator dashboard | Complete for localhost supervision | Authenticated FastAPI/Jinja dashboard and operator commands |
| Automated testing | Passing | 30 tests, including property-based sizing and regression coverage for per-instrument freshness, synchronized decision barriers, and sparse backtests |
| Static quality checks | Passing | Ruff and strict mypy pass |
| Real IBKR paper trading | Not yet demonstrated | Requires configured TWS/IB Gateway, paper account, live market-data permissions, and supervised smoke tests |
| Paper evidence collection | Not started | No claim yet of 90 sessions or 500 trades |
| Live trading | Deliberately blocked | `AI_TRADE_MODE=live` raises an error |
| Japanese equities | Planned | No adapter, calendar, universe, or strategy validation yet |
| Crypto | Planned | No exchange adapter, custody model, 24/7 session logic, or strategy validation yet |

### 3.2 Verified quality state

As of this document date:

- the project is on the `dev` branch;
- the working implementation is published to `origin/dev`;
- the test suite contains 30 passing tests;
- Ruff reports no lint failures;
- strict mypy reports no source typing failures;
- the application and research, backtest, and qualification command entry points have been smoke-tested locally;
- the initial Alembic migration has a valid head;
- recent code review findings covering stale per-symbol data, synchronized decision snapshots, and sparse final backtest bars have been fixed and regression-tested.

These results validate the local software contracts. They do not validate broker permissions, actual exchange data quality, realized fill behavior, network reliability, strategy profitability, or live-money safety.

### 3.3 Remaining work before meaningful paper operation

1. Install or start PostgreSQL through Docker Desktop, OrbStack, or an equivalent local service.
2. Apply the Alembic migration to the running database.
3. Configure a dedicated IBKR paper account ID and a non-default operator password.
4. Configure TWS or IB Gateway API access on the paper port.
5. Confirm the required US quote and real-time bar market-data permissions.
6. Run a read-only connectivity and subscription smoke test while the account is flat.
7. Validate contract resolution for every frozen universe instrument.
8. Exercise order submission, acknowledgement, partial fill, fill, rejection, bracket cancellation, commission, and disconnection paths in the paper account.
9. Compare broker positions, cash, executions, and commissions against the internal ledger.
10. Conduct stale-data, network-loss, database-failure, process-restart, rejection-rate, and mandatory-flat fault drills.

### 3.4 Remaining work before strategy evaluation

1. Obtain sufficiently long, point-in-time historical intraday datasets with survivorship and corporate-action handling documented.
2. Build strategy-specific cost-aware targets without leakage.
3. Establish fixed training, validation, and holdout periods.
4. Compare the rule baselines against trained models and simple null strategies.
5. Measure turnover, capacity, spread sensitivity, slippage sensitivity, drawdowns, and regime dependence.
6. Reject configurations that only work after excessive parameter searches.
7. Freeze model artifacts, feature versions, risk configuration, and universe version before paper evaluation.

### 3.5 Paper milestone and later roadmap

The next major milestone is a supervised US paper run. It should proceed in stages:

1. shadow observation with no submitted orders;
2. minimal-size paper orders and operational fault drills;
3. full configured USD 25,000 paper simulation;
4. at least 90 sessions and 500 aggregate trades;
5. strategy-level statistical and operational review;
6. a separate decision on whether to design a live adapter.

Only after the US milestone is stable should the project add new venues. The likely expansion order is:

1. Japanese cash equities with Japan-specific calendars, contract metadata, market data, tick sizes, lots, settlement, fees, and broker routing;
2. additional foreign-equity markets through explicit venue configurations;
3. crypto with a separate exchange/custody adapter, 24/7 scheduling, exchange-specific risk limits, and independent qualification evidence.

No roadmap stage automatically authorizes live trading. Each venue and execution mode requires its own safety review and evidence gate.
