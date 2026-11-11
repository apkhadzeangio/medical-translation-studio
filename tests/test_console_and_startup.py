import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def test_console_page_is_served_without_a_token(client):
    response = client.get("/")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "Medical Translation Studio" in response.text and "/api/projects" in response.text
    assert "sk-" not in response.text                      # nothing secret is baked into the page


def test_production_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="API_TOKEN"), TestClient(create_app()):
            pass
        monkeypatch.setenv("API_TOKEN", "long-random-value")
        get_settings.cache_clear()
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/api/projects").status_code == 401
    finally:
        monkeypatch.delenv("ENVIRONMENT")
        monkeypatch.delenv("API_TOKEN", raising=False)
        get_settings.cache_clear()
