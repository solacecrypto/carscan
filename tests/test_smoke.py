from fastapi.testclient import TestClient

from app.web.main import app


def test_healthz() -> None:
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}


def test_index_redirects_to_login() -> None:
    client = TestClient(app)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "Car Scanner" in client.get("/login").text
