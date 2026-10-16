"""Integration fixtures. Needs a real Postgres and Redis.

In CircleCI the service containers are on localhost. Locally, start them with
`docker compose up -d postgres redis` (and publish the ports) or set
DATABASE_URL / REDIS_URL yourself.
"""
import os
import pathlib
import time

import psycopg
import pytest
import redis

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://imagequeue:local_dev_password@localhost:5432/imagequeue")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "migrations"


def _wait(fn, what, seconds=60):
    deadline = time.time() + seconds
    while True:
        try:
            return fn()
        except Exception:
            if time.time() > deadline:
                raise RuntimeError(f"{what} never became ready")
            time.sleep(1)


@pytest.fixture(scope="session", autouse=True)
def database():
    conn = _wait(lambda: psycopg.connect(os.environ["DATABASE_URL"], autocommit=True),
                 "postgres")
    with conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(path.read_text())
    _wait(lambda: redis.Redis.from_url(os.environ["REDIS_URL"]).ping(), "redis")
    yield


@pytest.fixture(autouse=True)
def clean(database):
    """Empty Redis and drop every job except the five seeded ones."""
    redis.Redis.from_url(os.environ["REDIS_URL"]).flushdb()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DELETE FROM jobs WHERE id > 5")
    yield
