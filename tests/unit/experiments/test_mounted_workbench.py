import re

from fastapi.testclient import TestClient

from finbot.experiments.api import get_store
from finbot.experiments.executor import isolated_business_database
from finbot.experiments.store import Store


def test_mounted_page_session_csrf_and_launch(tmp_path):
    # No lifespan: neither Redis consumers nor a browser renderer is started.
    from finbot.main import app, vendor_app

    store = Store(tmp_path / "experiments.db")
    vendor_app.dependency_overrides[get_store] = lambda: store
    try:
        with isolated_business_database():
            client = TestClient(app)
            page = client.get("/vendor/experiments")
            assert page.status_code == 200, page.text[:1000]
            assert "experiments.js" in page.text
            token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
            assert token
            base = "/vendor/api/v1/experiments"
            connection = {"name": "Offline demo", "kind": "mock"}
            assert client.post(base + "/connections", json=connection).status_code == 403
            client.headers["X-CSRF-Token"] = token
            response = client.post(base + "/connections", json=connection)
            assert response.status_code == 201, response.text
            data = client.get(base + "/bootstrap").json()
            assert len(data["scenarios"]) == 7
            config = client.post(
                base + "/configs",
                json={
                    "name": "All seven",
                    "connection_id": response.json()["id"],
                    "model": "mock",
                },
            )
            assert config.status_code == 201, config.text
            run = client.post(base + "/runs", json={"config_id": config.json()["id"]})
            assert run.status_code == 202, run.text
            assert len(run.json()["cases"]) == 7
            assert client.get(base + "/runs").json()["runs"][0]["id"] == run.json()["id"]
            stranger = TestClient(app)
            assert stranger.get(base + "/runs/" + run.json()["id"]).status_code == 404
    finally:
        vendor_app.dependency_overrides.pop(get_store, None)
