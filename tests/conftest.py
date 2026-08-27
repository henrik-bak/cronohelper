"""Shared fixtures. Every test runs against the fake Cronometer client and a
throwaway SQLite file -- nothing here can reach the real API."""

from __future__ import annotations

import pytest

from app import db
from app.cronometer import CronometerAdapter
from tests.fake_cronometer import FakeCronometerClient


@pytest.fixture
def conn(tmp_path):
    """A fresh database per test, on disk so the real schema is exercised."""
    with db.connect(tmp_path / "test.sqlite3") as connection:
        yield connection


@pytest.fixture
def fake():
    return FakeCronometerClient()


@pytest.fixture
def adapter(fake):
    return CronometerAdapter(client=fake)
