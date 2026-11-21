"""tests/pipeline 的 fixtures。"""

import pytest
from pipeline_kit import FakeSite, install, new_env


@pytest.fixture
def site(monkeypatch) -> FakeSite:
    fake = FakeSite()
    install(monkeypatch, fake)
    return fake


@pytest.fixture
def env(site):
    environment = new_env()
    yield environment
    environment.conn.close()
