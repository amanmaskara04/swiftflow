from pathlib import Path

from swiftflow_common.config import BaseServiceSettings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "orders"
PRODUCER = "order"


class Settings(BaseServiceSettings):
    service_name: str = "order-api"
    database_url: str = "postgresql://order_svc:order-svc-dev@localhost:5432/swiftflow"
    payment_url: str = "http://localhost:8004"
    provider_api_key: str = "dev-provider-key-change-me"
    jwt_private_key_pem: str | None = None
    token_ttl_seconds: int = 900
    worker_port: int = 9101
    payment_timeout_seconds: float = 5.0
    payment_lease_seconds: int = 30


settings = Settings()
settings.require_real_secrets(provider_api_key=settings.provider_api_key, database_password=settings.database_url)
