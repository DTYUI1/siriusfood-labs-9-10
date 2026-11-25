"""Временный PostgreSQL с настоящими миграциями и изоляцией каждого теста."""
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer


@pytest.fixture
def session_factory(pytestconfig):
    # Docker выбирает свободный порт и публикует его только на loopback.
    postgres = PostgresContainer("postgres:15", username="sirius_integration",
                                 password=uuid4().hex, dbname="sirius_integration")
    postgres.with_bind_ports(5432, ("127.0.0.1", None))
    with postgres:
        database_url = postgres.get_connection_url()
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=pytestconfig.rootpath,
            env={**os.environ, "DATABASE_URL": database_url},
            check=True,
        )
        engine = create_engine(database_url)
        try:
            yield sessionmaker(bind=engine, autoflush=False)
        finally:
            engine.dispose()
