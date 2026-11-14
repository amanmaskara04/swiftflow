from pathlib import Path

from swiftflow_common.config import BaseServiceSettings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "notifications"


class Settings(BaseServiceSettings):
    service_name: str = "notification-api"
    database_url: str = "postgresql://notification_svc:notification-svc-dev@localhost:5432/swiftflow"
    # Development only: "host:port" entries that skip the public-IP check (e.g. the simulator).
    webhook_allowed_hosts: str = ""
    webhook_allow_http: bool = False
    webhook_timeout_seconds: float = 5.0
    worker_port: int = 9103
    max_subscriptions_per_tenant: int = 20

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.webhook_allowed_hosts.split(",") if h.strip())


settings = Settings()
