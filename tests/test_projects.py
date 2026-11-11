import uuid

from app.config import get_settings


def test_create_list_get(client):
    r = client.post("/api/projects", json={"name": "Vascular textbook", "budget_usd": "2500.00"})
    assert r.status_code == 201
    body = r.json()
    assert body["source_lang"] == "en" and body["target_lang"] == "ka"

    assert any(p["id"] == body["id"] for p in client.get("/api/projects").json())
    assert client.get(f"/api/projects/{body['id']}").json()["name"] == "Vascular textbook"
    assert client.get(f"/api/projects/{uuid.uuid4()}").status_code == 404


def test_validation(client):
    assert client.post("/api/projects", json={"name": ""}).status_code == 422
    assert client.post("/api/projects", json={"name": "x", "budget_usd": -1}).status_code == 422


def test_token_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    get_settings.cache_clear()
    try:
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/health").status_code == 200  # health stays open for Railway
    finally:
        monkeypatch.delenv("API_TOKEN")
        get_settings.cache_clear()
