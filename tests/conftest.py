import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from edu_app.api import build_app
from edu_app.config import Settings
from edu_app.migration import migrate
from edu_app.runtime import Runtime

_POSTGRES_URL = os.environ.get("EDU_TEST_DATABASE_URL")
_BACKENDS = ("sqlite", "postgresql") if _POSTGRES_URL else ("sqlite",)


@contextmanager
def _database_url(backend):
    if backend == "sqlite":
        yield None
        return
    url = make_url(_POSTGRES_URL)
    schema = f"edu_test_{uuid4().hex}"
    admin = create_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        try:
            test_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
            yield test_url.render_as_string(hide_password=False)
        finally:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
    finally:
        admin.dispose()


@pytest.fixture(params=_BACKENDS)
def runtime(tmp_path, request):
    with _database_url(request.param) as database_url:
        settings = Settings(
            _env_file=None,
            mode="local",
            data_dir=tmp_path,
            database_url=database_url,
            agent_url=None,
        )
        app_runtime = Runtime(settings)
        try:
            migrate(app_runtime.db)
            yield app_runtime
        finally:
            app_runtime.close()


@pytest.fixture
def client(runtime):
    with TestClient(build_app(runtime), headers={"X-Demo-Student": "demo-a"}) as test_client:
        yield test_client
