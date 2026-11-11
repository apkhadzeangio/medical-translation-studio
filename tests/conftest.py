import os

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://mts:mts@localhost:5432/mts_test")
os.environ["DATABASE_URL"] = TEST_URL
os.environ.pop("API_TOKEN", None)

from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def alembic_cfg() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="session", autouse=True)
def migrated_db(alembic_cfg):
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c
