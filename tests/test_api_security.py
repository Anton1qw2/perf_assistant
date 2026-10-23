import json

import pytest
from fastapi.testclient import TestClient

from performance_assistant.api import create_app
from performance_assistant.contracts import TaskSpec


def test_api_origin_and_validation_do_not_echo_secrets(system):
    config, *_ = system
    client = TestClient(create_app(config))
    assert (
        client.post(
            "/api/conversations", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    cid = client.post("/api/conversations", json={}).json()["id"]
    response = client.post(
        f"/api/conversations/{cid}/messages",
        json={"request_id": "a", "text": "ok", "secret": "DO_NOT_LEAK"},
    )
    assert response.status_code == 422
    assert "DO_NOT_LEAK" not in response.text
    assert client.get("/").status_code == 200
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]


def test_credential_encryption_binding_and_scrubbing(system):
    config, store, vault, service, worker, cid = system
    config.mcp_url = "https://profiler.example/mcp"
    settings = service.save_settings(
        cid, "settings", 0, {"repository": "repo"}, "TOKEN-VERY-SECRET"
    )
    stored = store.one("SELECT * FROM credentials")
    assert b"TOKEN-VERY-SECRET" not in stored["ciphertext"]
    assert vault.resolve(settings["credential_ref"], config.mcp_url, "repo") == "TOKEN-VERY-SECRET"
    with pytest.raises(ValueError):
        vault.resolve(settings["credential_ref"], config.mcp_url, "other")
    service.intake(cid, "message", "Hello TOKEN-VERY-SECRET")
    assert "TOKEN-VERY-SECRET" not in json.dumps(store.snapshot(cid))
    assert "TOKEN-VERY-SECRET" not in str(store.rows("SELECT * FROM events"))
    assert "TOKEN-VERY-SECRET" not in str(store.rows("SELECT * FROM requests"))


def test_http_credentials_rejected(system):
    _, store, vault, *_ = system
    with pytest.raises(ValueError), store.tx() as db:
        vault.save(db, "token", "http://localhost/mcp")


@pytest.mark.parametrize("profile", ["C:\\secret", "/tmp/profile", "../x", "http://host/p"])
def test_profile_is_not_a_path(profile):
    with pytest.raises(ValueError):
        TaskSpec(profile_id=profile)
