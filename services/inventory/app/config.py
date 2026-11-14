from pathlib import Path

from swiftflow_common.config import BaseServiceSettings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "inventory"
PRODUCER = "inventory"


class Settings(BaseServiceSettings):
    service_name: str = "inventory-api"
    database_url: str = "postgresql://inventory_svc:inventory-svc-dev@localhost:5432/swiftflow"
    availability_cache_ttl: int = 5
    worker_port: int = 9102


settings = Settings()
