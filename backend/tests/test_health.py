from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_reports_status_and_database() -> None:
    body = client.get("/health").json()
    assert body["status"] in {"ok", "degraded"}
    assert body["database"] in {"ok", "unavailable"}
    assert (body["status"] == "ok") == (body["database"] == "ok")
