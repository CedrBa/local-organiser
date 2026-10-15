import pytest
from fastapi.testclient import TestClient

from organiser.core.config import Settings
from organiser.services.knowledge import KnowledgeStore


TEST_API_TOKEN = "test-api-token"


@pytest.fixture(autouse=True)
def isolated_api_token_environment(monkeypatch):
    """Verhindert, dass ein echter Token aus der lokalen ENV in Tests einfliesst."""
    monkeypatch.delenv("ORGANISER_API_TOKEN", raising=False)
    monkeypatch.delenv("ORGANISER_KNOWLEDGE_ROOT", raising=False)


@pytest.fixture
def knowledge_root(tmp_path):
    """Temporaerer Knowledge Root; kein Test schreibt in das Repository."""
    root = tmp_path / "knowledge"
    root.mkdir()
    return root


@pytest.fixture
def knowledge_store(knowledge_root):
    return KnowledgeStore(knowledge_root)


def auth_headers(token: str = TEST_API_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class AuthenticatedTestClient(TestClient):
    """TestClient, der den gueltigen Test-Token standardmaessig mitsendet."""

    def __init__(self, *args, **kwargs):
        headers = {**auth_headers(), **(kwargs.pop("headers", None) or {})}
        super().__init__(*args, headers=headers, **kwargs)


def make_settings(tmp_path, filename: str = "test.sqlite3", **overrides) -> Settings:
    """Settings mit gesetztem Test-Token, damit die geschuetzten Routen nutzbar sind."""
    return Settings(database_path=tmp_path / filename, api_token=TEST_API_TOKEN, **overrides)
