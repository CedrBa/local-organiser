import pytest
from fastapi.testclient import TestClient

from conftest import TEST_API_TOKEN, auth_headers
from organiser.api.app import create_app
from organiser.core.config import Settings, get_settings


PROTECTED_REQUESTS = [
    ("POST", "/inbox"),
    ("POST", "/inbox-file"),
    ("POST", "/inbox/{inbox_id}/triage"),
    ("POST", "/inbox/{inbox_id}/interpret"),
    ("POST", "/tasks/{task_id}/plan"),
    ("POST", "/projects/{project_id}/plan"),
    ("GET", "/planning/proposals/{proposal_id}"),
    ("POST", "/planning/proposals/{proposal_id}/apply"),
    ("POST", "/scheduling/availability/normalize"),
    ("GET", "/calendar/calendars"),
    ("GET", "/calendar/busy"),
    ("GET", "/calendar/availability"),
    ("POST", "/calendar/scheduling/proposals"),
    ("POST", "/calendar/organiser/setup"),
    ("POST", "/calendar/scheduling/proposals/{proposal_id}/apply"),
    ("POST", "/scheduling/proposals"),
    ("POST", "/scheduling/replan"),
    ("GET", "/scheduling/proposals/{proposal_id}"),
    ("POST", "/scheduling/proposals/{proposal_id}/apply"),
    ("POST", "/planning-blocks"),
    ("GET", "/planning-blocks"),
    ("POST", "/projects"),
    ("GET", "/projects"),
    ("GET", "/projects/{project_id}"),
    ("PATCH", "/projects/{project_id}"),
    ("POST", "/projects/{project_id}/pause"),
    ("POST", "/projects/{project_id}/resume"),
    ("POST", "/projects/{project_id}/complete"),
    ("POST", "/projects/{project_id}/cancel"),
    ("POST", "/projects/{project_id}/reopen"),
    ("GET", "/projects/{project_id}/tasks"),
    ("POST", "/tasks"),
    ("GET", "/tasks"),
    ("GET", "/tasks/{task_id}"),
    ("PATCH", "/tasks/{task_id}"),
    ("POST", "/tasks/{task_id}/start"),
    ("POST", "/tasks/{task_id}/waiting"),
    ("POST", "/tasks/{task_id}/complete"),
    ("POST", "/tasks/{task_id}/reopen"),
    ("POST", "/tasks/{task_id}/cancel"),
    ("POST", "/tasks/{task_id}/reschedule"),
    ("GET", "/tasks/{task_id}/children"),
    ("GET", "/tasks/{task_id}/events"),
    ("GET", "/tasks/{task_id}/context"),
    ("GET", "/tasks/{task_id}/scheduling"),
    ("GET", "/projects/{project_id}/events"),
    ("GET", "/projects/{project_id}/context"),
    ("POST", "/contexts"),
    ("GET", "/contexts"),
    ("GET", "/contexts/{context_id}"),
    ("PATCH", "/contexts/{context_id}"),
    ("POST", "/contexts/{context_id}/status"),
    ("GET", "/contexts/{context_id}/children"),
    ("GET", "/contexts/{context_id}/events"),
    ("POST", "/contexts/{context_id}/sources"),
    ("GET", "/contexts/{context_id}/sources"),
    ("GET", "/contexts/{context_id}/sources/{source_id}"),
    ("PATCH", "/contexts/{context_id}/sources/{source_id}"),
    ("DELETE", "/contexts/{context_id}/sources/{source_id}"),
    ("GET", "/events"),
    # Context Workspace v1 (B ingestion, C topics/tasks, D builder, E actions)
    ("GET", "/context-action-proposals/{proposal_id}"),
    ("POST", "/context-action-proposals/{proposal_id}/apply"),
    ("POST", "/context-action-proposals/{proposal_id}/reject"),
    ("GET", "/contexts/{context_id}/action-proposals"),
    ("POST", "/contexts/{context_id}/action-proposals"),
    ("POST", "/contexts/{context_id}/knowledge/ensure"),
    ("GET", "/contexts/{context_id}/resources"),
    ("POST", "/contexts/{context_id}/sources/{source_id}/scan"),
    ("GET", "/contexts/{context_id}/topics"),
    ("POST", "/contexts/{context_id}/topics"),
    ("GET", "/contexts/{context_id}/workspace"),
    ("GET", "/resources/{resource_id}"),
    ("POST", "/resources/{resource_id}/extract"),
    ("POST", "/resources/{resource_id}/verify"),
    ("GET", "/tasks/{task_id}/resources"),
    ("POST", "/tasks/{task_id}/resources"),
    ("POST", "/tasks/{task_id}/resources/{resource_id}"),
    ("DELETE", "/tasks/{task_id}/resources/{resource_id}"),
    ("GET", "/topics/{topic_id}"),
    ("PATCH", "/topics/{topic_id}"),
    ("POST", "/topics/{topic_id}/knowledge"),
    ("POST", "/topics/{topic_id}/knowledge/ensure"),
    ("GET", "/topics/{topic_id}/resources"),
    ("POST", "/topics/{topic_id}/resources/{resource_id}"),
    ("DELETE", "/topics/{topic_id}/resources/{resource_id}"),
    ("POST", "/topics/{topic_id}/status"),
]


def build_client(
    tmp_path, filename: str = "auth.sqlite3", token: str | None = TEST_API_TOKEN
) -> TestClient:
    """TestClient ohne gesetzte Auth-Header; der Token wird bewusst per Hand mitgegeben."""
    return TestClient(create_app(Settings(database_path=tmp_path / filename, api_token=token)))


def test_protected_route_rejects_missing_token(tmp_path):
    with build_client(tmp_path) as client:
        response = client.get("/tasks")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert TEST_API_TOKEN not in response.text


def test_protected_route_rejects_wrong_token(tmp_path):
    with build_client(tmp_path) as client:
        response = client.get("/tasks", headers=auth_headers("wrong-token"))

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_route_accepts_correct_token(tmp_path):
    with build_client(tmp_path) as client:
        created = client.post("/projects", json={"title": "Hausarbeit"}, headers=auth_headers())
        listed = client.get("/tasks", headers=auth_headers())

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.parametrize(
    "params",
    [
        {"token": TEST_API_TOKEN},
        {"api_token": TEST_API_TOKEN},
    ],
)
def test_token_as_query_parameter_is_rejected(tmp_path, params):
    with build_client(tmp_path) as client:
        assert client.get("/tasks", params=params).status_code == 401
        assert client.post("/inbox", params=params, json={}).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        f"Basic {TEST_API_TOKEN}",
        f"Bearer {TEST_API_TOKEN}x",
        f"Bearer {TEST_API_TOKEN[:-1]}",
        f"bearer {TEST_API_TOKEN}x",
        "Bearer",
        TEST_API_TOKEN,
    ],
)
def test_malformed_or_wrong_authorization_headers_are_rejected(tmp_path, header):
    with build_client(tmp_path) as client:
        response = client.get("/tasks", headers={"Authorization": header})

    assert response.status_code == 401


def test_bearer_scheme_is_case_insensitive(tmp_path):
    with build_client(tmp_path) as client:
        assert client.get("/tasks", headers=auth_headers()).status_code == 200
        assert (
            client.get("/tasks", headers={"Authorization": f"bEaReR {TEST_API_TOKEN}"}).status_code
            == 200
        )


@pytest.mark.parametrize(("method", "path"), PROTECTED_REQUESTS)
def test_business_routes_reject_requests_without_token(tmp_path, method, path):
    with build_client(tmp_path) as client:
        response = client.request(method, path)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED_REQUESTS)
def test_business_routes_fail_closed_without_configured_token(tmp_path, method, path):
    with build_client(tmp_path, "no-token-matrix.sqlite3", token=None) as client:
        assert client.request(method, path).status_code == 503
        assert (
            client.request(method, path, headers=auth_headers("irgend-ein-token")).status_code
            == 503
        )


def test_every_registered_business_route_is_protected(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "routes.sqlite3", api_token=TEST_API_TOKEN))
    openapi_paths = app.openapi()["paths"]

    assert "/health" in openapi_paths
    assert set(openapi_paths) - {"/health"} == {path for _, path in PROTECTED_REQUESTS}
    registered = {
        (method.upper(), path)
        for path, operations in openapi_paths.items()
        for method in operations
    }
    assert registered - {("GET", "/health")} == set(PROTECTED_REQUESTS)
    with TestClient(app) as client:
        for path, operations in openapi_paths.items():
            if path == "/health":
                continue
            for method in operations:
                assert client.request(method, path).status_code == 401, f"{method} {path}"


def test_health_is_reachable_without_authentication(tmp_path):
    with build_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_leak_internal_information(tmp_path):
    app = create_app(
        Settings(
            database_path=tmp_path / "interner-datenbankname.sqlite3", api_token=TEST_API_TOKEN
        )
    )
    with TestClient(app) as client:
        response = client.get("/health")

    assert set(response.json()) == {"status"}
    assert "interner-datenbankname" not in response.text
    assert "organiser" not in response.text.lower()
    assert TEST_API_TOKEN not in response.text


def test_health_stays_available_when_no_token_is_configured(tmp_path):
    with build_client(tmp_path, "no-token.sqlite3", token=None) as client:
        assert client.get("/health").status_code == 200


def test_protected_routes_fail_closed_without_configured_token(tmp_path):
    with build_client(tmp_path, "no-token.sqlite3", token=None) as client:
        without_header = client.get("/tasks")
        with_header = client.get("/tasks", headers=auth_headers("irgend-ein-token"))

    assert without_header.status_code == 503
    assert with_header.status_code == 503
    assert "ORGANISER_API_TOKEN" in without_header.json()["detail"]


def test_settings_reads_api_token_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ORGANISER_DATABASE_PATH", str(tmp_path / "env.sqlite3"))
    monkeypatch.setenv("ORGANISER_API_TOKEN", f"  {TEST_API_TOKEN}  ")

    assert get_settings().api_token == TEST_API_TOKEN


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_environment_token_is_treated_as_missing(monkeypatch, tmp_path, value):
    monkeypatch.setenv("ORGANISER_DATABASE_PATH", str(tmp_path / "env.sqlite3"))
    monkeypatch.setenv("ORGANISER_API_TOKEN", value)

    settings = get_settings()

    assert settings.api_token is None
    with TestClient(create_app(settings)) as client:
        assert client.get("/tasks").status_code == 503


def test_settings_repr_does_not_expose_the_api_token(tmp_path):
    settings = Settings(database_path=tmp_path / "auth.sqlite3", api_token=TEST_API_TOKEN)

    assert "api_token" not in repr(settings)
    assert TEST_API_TOKEN not in repr(settings)


def test_openapi_declares_the_bearer_scheme(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "auth.sqlite3", api_token=TEST_API_TOKEN))
    openapi = app.openapi()

    assert openapi["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Statischer Bearer-Token aus ORGANISER_API_TOKEN.",
    }
    assert openapi["paths"]["/tasks"]["get"]["security"] == [{"HTTPBearer": []}]
