from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


class BrokerSettings(BaseModel):
    account_id: str = ""
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 17
    connect_timeout_seconds: int = 15


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://ai_trade:ai_trade@localhost:5432/ai_trade"
    echo: bool = False


class RiskSettings(BaseModel):
    starting_nav: Decimal = Decimal("25000")
    maximum_gross_fraction: Decimal = Decimal("0.50")
    maximum_position_fraction: Decimal = Decimal("0.125")
    maximum_sleeve_fraction: Decimal = Decimal("0.25")
    maximum_positions: int = 4
    maximum_positions_per_sleeve: int = 2
    risk_per_trade_fraction: Decimal = Decimal("0.0015")
    daily_loss_fraction: Decimal = Decimal("0.0075")
    maximum_entry_slippage_bps: Decimal = Decimal("10")
    data_stale_seconds: int = 10
    rejection_window_seconds: int = 60
    rejection_kill_count: int = 3


class StrategySettings(BaseModel):
    decision_interval_minutes: int = 5
    decision_collection_timeout_seconds: float = Field(default=1.0, gt=0, le=10)
    maximum_holding_minutes: int = 60
    momentum_start_et: str = "09:45"
    momentum_last_entry_et: str = "14:45"
    mean_reversion_start_et: str = "10:00"
    mean_reversion_last_entry_et: str = "15:00"
    timed_exit_et: str = "15:35"
    flat_et: str = "15:45"
    assumed_slippage_bps_per_side: Decimal = Decimal("3")


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AI_TRADE_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    mode: TradingMode = TradingMode.PAPER
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    operator_password: SecretStr = SecretStr("replace-me")
    universe_path: str = "config/universe.json"

    def assert_safe(self) -> None:
        if self.mode is TradingMode.LIVE:
            raise RuntimeError("Live trading is disabled in this release")
        if self.mode is TradingMode.PAPER and not self.broker.account_id:
            raise RuntimeError("AI_TRADE_BROKER__ACCOUNT_ID is required in paper mode")
        if self.operator_password.get_secret_value() == "replace-me":
            raise RuntimeError("AI_TRADE_OPERATOR_PASSWORD must be changed from its default")
