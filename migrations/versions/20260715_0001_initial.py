"""Initial trading ledger and audit schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260715_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_market_events_instrument_time",
        "market_events",
        ["instrument_id", "event_at"],
    )
    op.create_table(
        "bars",
        sa.Column("instrument_id", sa.String(64), primary_key=True),
        sa.Column("interval_seconds", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 10), nullable=False),
        sa.Column("high", sa.Numeric(24, 10), nullable=False),
        sa.Column("low", sa.Numeric(24, 10), nullable=False),
        sa.Column("close", sa.Numeric(24, 10), nullable=False),
        sa.Column("volume", sa.Numeric(28, 8), nullable=False),
        sa.Column("bid", sa.Numeric(24, 10)),
        sa.Column("ask", sa.Numeric(24, 10)),
        sa.Column("complete", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_bars_instrument_end", "bars", ["instrument_id", "ended_at"])
    op.create_table(
        "order_intents",
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "risk_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_risk_decisions_intent_id", "risk_decisions", ["intent_id"])
    op.create_table(
        "broker_orders",
        sa.Column("internal_order_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("broker_order_id", sa.String(64), unique=True),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_broker_orders_intent_id", "broker_orders", ["intent_id"])
    op.create_index("ix_broker_orders_state", "broker_orders", ["state"])
    op.create_table(
        "executions",
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("broker_execution_id", sa.String(128), nullable=False, unique=True),
        sa.Column("internal_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_executions_internal_order_id", "executions", ["internal_order_id"])
    op.create_table(
        "audit_log",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_audit_log_event_at", "audit_log", ["event_at"])
    op.create_table(
        "outbox",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_outbox_topic", "outbox", ["topic"])


def downgrade() -> None:
    for table in (
        "outbox",
        "audit_log",
        "executions",
        "broker_orders",
        "risk_decisions",
        "order_intents",
        "bars",
        "market_events",
    ):
        op.drop_table(table)
