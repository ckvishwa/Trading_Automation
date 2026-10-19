"""Typed runtime configuration.

Secrets are read from the environment only; nothing sensitive is stored in source.
`TRADING_MODE` is validated at import of the settings object: the engine refuses to
run in any mode other than PAPER, because V1 contains no live-order code path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # --- Mode -------------------------------------------------------------
    # Deliberately a Literal with a single member: there is no toggle that can
    # enable real trading, by configuration or otherwise.
    trading_mode: Literal["PAPER"] = Field(default="PAPER", alias="TRADING_MODE")

    # --- Webhook ingress --------------------------------------------------
    webhook_token: str = Field(default="", alias="WEBHOOK_TOKEN")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8788, alias="API_PORT")
    max_body_bytes: int = Field(default=8192, alias="MAX_BODY_BYTES")

    # --- Signal contract --------------------------------------------------
    expected_indicator: str = Field(
        default="Half Trend Regression [AlgoAlpha]", alias="EXPECTED_INDICATOR"
    )
    allowed_symbols: str = Field(default="NSE:NIFTY", alias="ALLOWED_SYMBOLS")
    allowed_timeframes: str = Field(default="1m,3m,5m,15m", alias="ALLOWED_TIMEFRAMES")
    # Three independent budgets that the earlier single 10-second tolerance was
    # conflating. They answer different questions and must not share a value:
    #
    # * max_tradable_age_seconds -- how late a delivery may be and still be
    #   allowed to open a paper position. This is a trading-risk decision.
    # * max_recordable_age_seconds -- how late a delivery may be and still be
    #   worth storing for analysis. Beyond this the event is rejected outright.
    # * clock_skew_tolerance_seconds -- how far a timestamp may sit in the
    #   future before it is treated as a clock or feed fault.
    # * bar_close_tolerance_seconds -- how far before its own bar close an alert
    #   may claim to have fired. This is candle alignment, not delivery.
    max_tradable_age_seconds: int = Field(default=120, alias="MAX_TRADABLE_AGE_SECONDS")
    max_recordable_age_seconds: int = Field(
        default=900, alias="MAX_RECORDABLE_AGE_SECONDS"
    )
    clock_skew_tolerance_seconds: int = Field(
        default=10, alias="CLOCK_SKEW_TOLERANCE_SECONDS"
    )
    bar_close_tolerance_seconds: int = Field(
        default=5, alias="BAR_CLOSE_TOLERANCE_SECONDS"
    )

    # --- Market data credentials -----------------------------------------
    # Read from the environment only, never from a file in the repository and
    # never from a prompt. Both are registered with the log redactor at startup,
    # so neither can reach a log line. Absent means LIVE BLOCKED, not degraded
    # live operation: an engine that cannot authenticate must say so.
    upstox_access_token: str = Field(default="", alias="UPSTOX_ACCESS_TOKEN")
    upstox_api_key: str = Field(default="", alias="UPSTOX_API_KEY")
    fyers_access_token: str = Field(default="", alias="FYERS_ACCESS_TOKEN")

    # --- Storage ----------------------------------------------------------
    database_path: Path = Field(
        default=PROJECT_ROOT / "data" / "engine.db", alias="DATABASE_PATH"
    )
    holiday_file: Path = Field(
        default=PROJECT_ROOT / "data" / "reference" / "nse_calendar.json",
        alias="HOLIDAY_FILE",
    )

    @field_validator("database_path", "holiday_file")
    @classmethod
    def _anchor_to_project_root(cls, value: Path) -> Path:
        # A relative path in .env means "relative to the project", not to
        # whichever directory the process happened to start in.
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("max_recordable_age_seconds")
    @classmethod
    def _recordable_covers_tradable(cls, value: int, info) -> int:
        tradable = info.data.get("max_tradable_age_seconds")
        if tradable is not None and value < tradable:
            raise ValueError(
                "MAX_RECORDABLE_AGE_SECONDS must be at least "
                "MAX_TRADABLE_AGE_SECONDS; a signal cannot be fresh enough to "
                "trade but too old to record"
            )
        return value

    @field_validator("webhook_token")
    @classmethod
    def _token_must_be_strong(cls, value: str) -> str:
        if value and (len(value) < 32 or value.upper().startswith("REPLACE")):
            raise ValueError(
                "WEBHOOK_TOKEN must be at least 32 characters of unguessable randomness"
            )
        return value

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in a log line."""
        return tuple(
            value
            for value in (
                self.webhook_token,
                self.upstox_access_token,
                self.upstox_api_key,
                self.fyers_access_token,
            )
            if value
        )

    @property
    def has_upstox_credentials(self) -> bool:
        return bool(self.upstox_access_token)

    @property
    def symbol_allowlist(self) -> tuple[str, ...]:
        return tuple(s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip())

    @property
    def timeframe_allowlist(self) -> tuple[str, ...]:
        return tuple(t.strip().lower() for t in self.allowed_timeframes.split(",") if t.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
