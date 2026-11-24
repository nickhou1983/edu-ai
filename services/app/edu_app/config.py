from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDU_", env_file=".env", extra="ignore")

    mode: Literal["local", "azure"]
    data_dir: Path = Path(".edu-ai")
    web_dir: Path = Path("apps/web/dist")
    database_url: str | None = None
    agent_url: str | None = None
    agent_timeout_seconds: int = Field(default=125, ge=10, le=130)
    managed_identity_client_id: UUID | None = None
    blob_account_url: str | None = None
    blob_container: str | None = None
    service_bus_namespace: str | None = None
    service_bus_queue: str | None = None
    entra_tenant_id: UUID | None = None
    entra_api_client_id: UUID | None = None
    entra_spa_client_id: UUID | None = None

    @model_validator(mode="after")
    def check_mode(self):
        if self.mode == "local":
            if self.agent_url:
                url = urlsplit(self.agent_url)
                if url.scheme not in ("http", "https") or url.hostname not in (
                    "localhost",
                    "127.0.0.1",
                    "::1",
                ):
                    raise ValueError("Local mode only sends images to a loopback Agent endpoint")
            return self
        for name in (
            "database_url",
            "agent_url",
            "blob_account_url",
            "blob_container",
            "service_bus_namespace",
            "service_bus_queue",
            "entra_tenant_id",
            "entra_api_client_id",
            "entra_spa_client_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"Azure mode requires EDU_{name.upper()}")
        database = make_url(self.database_url)
        if (
            database.drivername != "postgresql+psycopg"
            or not database.host
            or not database.username
            or database.password
        ):
            raise ValueError(
                "Azure mode requires PostgreSQL and managed identity, without a database password"
            )
        agent = urlsplit(self.agent_url)
        blob = urlsplit(self.blob_account_url)
        if (
            agent.scheme != "https"
            or not (agent.hostname or "").endswith(".services.ai.azure.com")
            or not agent.path.rstrip("/").endswith("/endpoint/protocols/invocations")
            or blob.scheme != "https"
            or not (blob.hostname or "").endswith(".blob.core.windows.net")
            or not self.service_bus_namespace.endswith(".servicebus.windows.net")
        ):
            raise ValueError("Azure mode requires Microsoft Foundry, Blob Storage, and Service Bus endpoints")
        return self

    @property
    def dsn(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir.resolve() / 'state.sqlite3'}"
