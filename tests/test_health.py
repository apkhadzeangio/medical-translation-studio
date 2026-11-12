def test_liveness(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_reports_revision(client):
    r = client.get("/health/db")
    assert r.status_code == 200
    assert r.json()["revision"] == "0005"


def test_readiness_fails_when_schema_is_behind_the_code(client, monkeypatch):
    from app.api import health
    monkeypatch.setattr(health, "expected_revision", lambda: "9999")
    r = client.get("/health/db")
    assert r.status_code == 503 and "0005" in r.json()["detail"] and "9999" in r.json()["detail"]


def test_expected_revision_is_read_from_the_migration_scripts():
    from app.api.health import expected_revision
    assert expected_revision() == "0005"


def test_worker_health_without_embedded_worker(client):
    r = client.get("/health/worker")
    assert r.status_code == 200
    body = r.json()
    assert body["embedded"] is False and body["threads"] == 0 and isinstance(body["queue"], dict)


def test_worker_health_with_embedded_worker(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app
    monkeypatch.setenv("EMBEDDED_WORKER", "true")
    monkeypatch.setenv("WORKER_THREADS", "2")
    monkeypatch.setenv("LLM_API_KEYS", "sk-test-not-a-real-key")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            r = client.get("/health/worker")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ok" and r.json()["alive"] == 2 and r.json()["llm_keys"] == 1
            assert "sk-test" not in r.text                      # the key itself is never reported
            for worker, _ in client.app.state.workers:          # a worker that cannot reach its queue
                worker.last_loop_error = "ProgrammingError"
            r = client.get("/health/worker")
            assert r.status_code == 503 and r.json()["detail"]["loop_errors"] == ["ProgrammingError"]
    finally:
        monkeypatch.delenv("EMBEDDED_WORKER")
        monkeypatch.delenv("WORKER_THREADS")
        monkeypatch.delenv("LLM_API_KEYS")
        get_settings.cache_clear()
