def test_liveness(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_reports_revision(client):
    r = client.get("/health/db")
    assert r.status_code == 200
    assert r.json()["revision"] == "0001"
