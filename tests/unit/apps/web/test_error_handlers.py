import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from finbot.core.error_handlers import is_api_request, register_error_handlers


@pytest.mark.parametrize(
    "path,root_path,expected",
    [
        ("/api/v1/test", "", True),
        ("/vendor/api/v1/test", "", True),
        ("/ctf/api/v1/test", "/ctf", True),
        ("/admin/api/v1/test", "/admin", True),
        ("/prefix/vendor/api/v1/test", "/prefix/vendor", True),
        ("/prefix/vendor/api/v1/test", "/prefix", True),
        ("/vendor/api/v1/test", "/prefix", True),
        ("/api/v1/test", "/prefix/vendor", True),
        ("/vendor/experiments", "/vendor", False),
        ("/vendor/apiary", "/vendor", False),
        ("/vendor-other/api/v1/test", "/vendor", False),
        ("/help/api/examples", "", False),
    ],
)
def test_api_paths_respect_mounts_and_path_boundaries(path, root_path, expected):
    request = Request({"type": "http", "path": path, "root_path": root_path})
    assert is_api_request(request) is expected


def error_app():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/http/{status}")
    @app.get("/page/http/{status}")
    async def http_error(status: int):
        raise HTTPException(status, "Endpoint validation failed")

    @app.get("/api/validate")
    @app.get("/page/validate")
    async def validate(count: int):
        return {"count": count}

    @app.get("/api/csrf")
    async def csrf():
        raise HTTPException(403, "CSRF token missing")

    @app.get("/api/crash")
    async def crash():
        raise RuntimeError("secret-test-key must not be exposed")

    return app


@pytest.fixture(params=["", "/prefix"])
def mounted_client(request):
    app = error_app()
    app.mount("/vendor", error_app())
    return TestClient(app, root_path=request.param, raise_server_exceptions=False), request.param


@pytest.mark.parametrize("status", [401, 403, 422])
def test_mounted_api_http_errors_keep_status_and_message(mounted_client, status):
    client, prefix = mounted_client
    response = client.get(f"{prefix}/vendor/api/http/{status}")
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["message"] == "Endpoint validation failed"


def test_mounted_api_missing_route_and_csrf_are_json(mounted_client):
    client, prefix = mounted_client
    response = client.get(f"{prefix}/vendor/api/not-found")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == 404
    response = client.get(f"{prefix}/vendor/api/csrf")
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "csrf_error"


def test_mounted_api_validation_error_is_json_without_input(mounted_client):
    client, prefix = mounted_client
    response = client.get(f"{prefix}/vendor/api/validate", params={"count": "secret-test-key"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["details"][0]["field"] == "query -> count"
    assert "secret-test-key" not in response.text


def test_unexpected_api_error_does_not_expose_exception(mounted_client):
    client, prefix = mounted_client
    for path in ["/api/crash", "/vendor/api/crash"]:
        response = client.get(prefix + path)
        assert response.status_code == 500
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"]["message"] == "Internal Server Error"
        assert "secret-test-key" not in response.text


def test_web_errors_remain_html(mounted_client):
    client, prefix = mounted_client
    for path in ["/vendor/page/http/403", "/vendor/page/validate", "/help/api/examples"]:
        response = client.get(prefix + path, headers={"Accept": "application/json"})
        assert response.status_code in [400, 403, 404]
        assert response.headers["content-type"].startswith("text/html")
