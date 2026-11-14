"""Settings shared by every service (pydantic-settings, environment variables)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common settings. Each service subclasses this and adds its own fields.

    Values come from environment variables, then from ``.env.local`` in the
    working directory (hybrid mode), then from the defaults below.
    """

    model_config = SettingsConfigDict(env_file=".env.local", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "swiftflow"
    environment: str = "local"  # local | ci | production
    log_level: str = "INFO"
    log_format: str = "console"  # console | json
    version: str = "0.1.0"

    redis_url: str = "redis://127.0.0.1:6379/0"
    kafka_bootstrap: str = "127.0.0.1:9092"
    otel_exporter_otlp_endpoint: str | None = None

    jwt_issuer: str = "swiftflow-order"
    jwt_audience: str = "swiftflow"
    jwks_url: str = "http://localhost:8001/.well-known/jwks.json"

    rate_limit_rps: float = 200.0
    rate_limit_burst: int = 1000

    max_body_bytes: int = 1_048_576

    @property
    def is_dev(self) -> bool:
        return self.environment in {"local", "ci"}

    def require_real_secrets(self, **values: str | None) -> None:
        """Fail fast if a production process was given a development default."""
        if self.is_dev:
            return
        weak = [name for name, v in values.items() if not v or "dev" in v.lower() or "change-me" in v.lower() or len(v) < 16]
        if weak:
            raise RuntimeError(f"refusing to start in {self.environment}: set real values for {', '.join(sorted(weak))}")
