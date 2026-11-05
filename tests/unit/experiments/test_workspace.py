import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finbot.core.auth.middleware import get_session_context
from finbot.core.auth.session import SessionContext
from finbot.experiments.api import get_store, router
from finbot.experiments.store import Store
from finbot.experiments import workspace

REGISTERED_OWNER = workspace.registered_owner


def browser(path, user, *, temporary=True, cookies=None):
    app = FastAPI()
    app.include_router(router)
    now = datetime.now(UTC)
    context = SessionContext(
        user + "-session", user, temporary, "ns_" + user, now, now + timedelta(days=1)
    )
    app.dependency_overrides[get_session_context] = lambda: context
    app.dependency_overrides[get_store] = lambda: Store(path)
    client = TestClient(app)
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value, domain="testserver.local", path="/")
    return client


@pytest.fixture(autouse=True)
def guest_registry(monkeypatch):
    monkeypatch.setattr(workspace, "registered_owner", lambda owner: False)


def save_run(client):
    connection = client.post(
        "/api/v1/experiments/connections",
        json={
            "name": "demo",
            "kind": "mock",
            "api_key": "private-test-key",
        },
    )
    assert connection.status_code == 201, connection.text
    config = client.post(
        "/api/v1/experiments/configs",
        json={
            "name": "saved settings",
            "connection_id": connection.json()["id"],
            "model": "mock",
            "temperature": 0.3,
            "scenario_ids": ["baseline_clean"],
        },
    )
    assert config.status_code == 201, config.text
    run = client.post("/api/v1/experiments/runs", json={"config_id": config.json()["id"]})
    assert run.status_code == 202, run.text
    return connection.json(), config.json(), run.json()


def test_restart_and_replaced_guest_session_preserve_workspace(tmp_path):
    path = tmp_path / "experiments.db"
    original = browser(path, "first")
    connection, config, run = save_run(original)
    cookie = original.cookies.get(workspace.COOKIE)
    assert cookie and len(cookie) == 43
    original.close()

    restored = browser(path, "new-session-user", cookies={workspace.COOKIE: cookie})
    data = restored.get("/api/v1/experiments/bootstrap")
    assert data.json()["configs"][0]["id"] == config["id"]
    assert data.json()["configs"][0]["temperature"] == 0.3
    assert data.json()["connections"][0]["id"] == connection["id"]
    assert restored.get("/api/v1/experiments/runs").json()["runs"][0]["id"] == run["id"]
    assert "private-test-key" not in data.text
    assert "HttpOnly" in data.headers["set-cookie"]
    assert "SameSite=lax" in data.headers["set-cookie"]
    assert "Max-Age=31536000" in data.headers["set-cookie"]
    store = Store(path)
    owner = json.dumps(["ns_first", "first"], separators=(",", ":"))
    assert (
        store.get_connection(owner, connection["id"], secret=True)["api_key"] == "private-test-key"
    )
    with store.db() as db:
        hashes = [row["token_hash"] for row in db.execute("SELECT token_hash FROM workspaces")]
    assert cookie not in hashes
    assert hashlib.sha256(cookie.encode()).hexdigest() in hashes


@pytest.mark.parametrize("bad_cookie", [None, "x" * 43, "x" * 10000])
def test_other_browser_cannot_read_or_mutate_existing_workspace(tmp_path, bad_cookie):
    path = tmp_path / "experiments.db"
    original = browser(path, "first")
    connection, config, run = save_run(original)
    cookies = {workspace.COOKIE: bad_cookie} if bad_cookie else None
    stranger = browser(path, "stranger", cookies=cookies)
    assert stranger.get("/api/v1/experiments/bootstrap").json()["configs"] == []
    assert stranger.get("/api/v1/experiments/runs/" + run["id"]).status_code == 404
    assert stranger.post("/api/v1/experiments/runs/" + run["id"] + "/cancel").status_code == 404
    assert (
        stranger.post("/api/v1/experiments/runs", json={"config_id": config["id"]}).status_code
        == 404
    )
    assert (
        stranger.post(
            "/api/v1/experiments/connections/" + connection["id"] + "/discover"
        ).status_code
        == 404
    )


def test_expired_workspace_does_not_restore_history(tmp_path):
    path = tmp_path / "experiments.db"
    original = browser(path, "first")
    _, _, run = save_run(original)
    with Store(path).db() as db:
        db.execute("UPDATE workspaces SET expires=0")
    replacement = browser(
        path, "replacement", cookies={workspace.COOKIE: original.cookies.get(workspace.COOKIE)}
    )
    assert replacement.get("/api/v1/experiments/runs").json()["runs"] == []
    assert replacement.get("/api/v1/experiments/runs/" + run["id"]).status_code == 404


def test_permanent_account_never_inherits_guest_cookie(tmp_path):
    path = tmp_path / "experiments.db"
    guest = browser(path, "guest")
    _, _, run = save_run(guest)
    permanent = browser(
        path,
        "account",
        temporary=False,
        cookies={workspace.COOKIE: guest.cookies.get(workspace.COOKIE)},
    )
    assert permanent.get("/api/v1/experiments/runs").json()["runs"] == []
    assert permanent.get("/api/v1/experiments/runs/" + run["id"]).status_code == 404
    assert permanent.cookies.get(workspace.COOKIE) is None


def test_guest_cookie_cannot_restore_account_upgraded_in_place(tmp_path, monkeypatch):
    path = tmp_path / "experiments.db"
    guest = browser(path, "first")
    _, _, run = save_run(guest)
    old_cookie = guest.cookies.get(workspace.COOKIE)
    # The same user_id/namespace becomes a registered account after email binding.
    monkeypatch.setattr(
        workspace, "registered_owner", lambda owner: json.loads(owner)[1] == "first"
    )
    replacement = browser(path, "replacement", cookies={workspace.COOKIE: old_cookie})
    assert replacement.get("/api/v1/experiments/runs/" + run["id"]).status_code == 404
    assert Store(path).workspace(old_cookie) is None
    permanent = browser(path, "first", temporary=False)
    assert permanent.get("/api/v1/experiments/runs/" + run["id"]).status_code == 200


def test_current_session_existing_data_visible_before_cookie_was_added(tmp_path):
    path = tmp_path / "experiments.db"
    owner = json.dumps(["ns_first", "first"], separators=(",", ":"))
    store = Store(path)
    item = store.save_connection(owner, {"name": "existing", "kind": "mock"})
    client = browser(path, "first")
    response = client.get("/api/v1/experiments/bootstrap")
    assert response.json()["connections"][0]["id"] == item["id"]
    assert client.cookies.get(workspace.COOKIE)


def test_registered_owner_uses_both_user_and_namespace():
    from finbot.experiments.executor import isolated_business_database
    from finbot.core.data.models import User
    from finbot.core.data import database

    with isolated_business_database():
        with database.SessionLocal() as db:
            db.add(User(user_id="bound", namespace="ns_bound", email="bound@example.invalid"))
            db.commit()
        assert REGISTERED_OWNER(json.dumps(["ns_bound", "bound"]))
        assert not REGISTERED_OWNER(json.dumps(["wrong", "bound"]))
        assert not REGISTERED_OWNER(json.dumps(["ns_guest", "guest"]))
