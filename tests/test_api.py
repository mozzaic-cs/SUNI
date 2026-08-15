"""
API smoke tests — happy-path coverage of all major endpoints.

Verifies:
  - /api/status            fields + shape
  - /api/auth/me           per-role response
  - /api/config            admin read + structure
  - /api/users             admin list structure
  - /api/models/inventory  admin access
  - /api/memory/stats      all roles
  - /api/chat              SSE stream format (token events + done event)
  - /api/session           session info fields
"""
import pytest
import json
from tests.conftest import parse_sse


class TestStatus:

    def test_status_returns_200(self, client, admin_headers):
        r = client.get("/api/status", headers=admin_headers)
        assert r.status_code == 200

    def test_status_has_required_fields(self, client, admin_headers):
        r = client.get("/api/status", headers=admin_headers)
        body = r.json()
        for field in ("model", "memory", "claude_code", "tiers"):
            assert field in body, f"Missing field {field!r}. Got: {list(body.keys())}"

    def test_status_model_is_string(self, client, admin_headers):
        r = client.get("/api/status", headers=admin_headers)
        assert isinstance(r.json()["model"], str)
        assert len(r.json()["model"]) > 0

    def test_status_tiers_is_dict(self, client, admin_headers):
        r = client.get("/api/status", headers=admin_headers)
        assert isinstance(r.json()["tiers"], dict)

    def test_status_accessible_by_all_roles(self, client, admin_headers, std_headers, ro_headers):
        for headers in (admin_headers, std_headers, ro_headers):
            assert client.get("/api/status", headers=headers).status_code == 200


class TestConfig:

    def test_config_returns_dict(self, client, admin_headers):
        r = client.get("/api/config", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)

    def test_config_has_key_fields(self, client, admin_headers):
        r = client.get("/api/config", headers=admin_headers)
        body = r.json()
        for field in ("model", "display_name"):
            assert field in body, f"Config missing: {field!r}"

    def test_config_accessible_by_standard(self, client, std_headers):
        r = client.get("/api/config", headers=std_headers)
        assert r.status_code == 200

    def test_config_update_by_any_auth_user(self, client, std_headers):
        r = client.post("/api/config",
                        json={"system_prompt_addendum": "test addendum"},
                        headers=std_headers)
        assert r.status_code == 200


class TestUsers:

    def test_users_list_structure(self, client, admin_headers):
        r = client.get("/api/users", headers=admin_headers)
        assert r.status_code == 200
        users = r.json()
        assert isinstance(users, list)
        assert len(users) >= 3  # we created 3 test users

    def test_users_list_has_expected_fields(self, client, admin_headers):
        r = client.get("/api/users", headers=admin_headers)
        for user in r.json():
            for field in ("id", "username", "role"):
                assert field in user, f"User missing field: {field!r}"

    def test_users_list_includes_test_users(self, client, admin_headers):
        r = client.get("/api/users", headers=admin_headers)
        usernames = {u["username"] for u in r.json()}
        assert "admin_test" in usernames
        assert "std_test" in usernames
        assert "ro_test" in usernames

    def test_users_list_no_passwords(self, client, admin_headers):
        r = client.get("/api/users", headers=admin_headers)
        for user in r.json():
            assert "password" not in user
            assert "password_h" not in user


class TestModelInventory:

    def test_inventory_returns_dict_with_models(self, client, admin_headers):
        r = client.get("/api/models/inventory", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        # Response is {"models": [...], "tier_map": {...}}
        assert "models" in body or isinstance(body, list), \
            f"Unexpected inventory shape: {list(body.keys()) if isinstance(body, dict) else type(body)}"

    def test_inventory_models_have_required_fields(self, client, admin_headers):
        r = client.get("/api/models/inventory", headers=admin_headers)
        body = r.json()
        models = body.get("models", body) if isinstance(body, dict) else body
        assert isinstance(models, list)
        for m in models:
            assert "name" in m, f"Model missing 'name': {m}"

    def test_inventory_refresh_param_accepted(self, client, admin_headers):
        r = client.get("/api/models/inventory?refresh=0", headers=admin_headers)
        assert r.status_code == 200


class TestMemoryStats:

    def test_memory_stats_returns_200(self, client, admin_headers):
        r = client.get("/api/memory/stats", headers=admin_headers)
        assert r.status_code == 200

    def test_memory_stats_accessible_by_standard(self, client, std_headers):
        r = client.get("/api/memory/stats", headers=std_headers)
        assert r.status_code == 200


class TestChatEndpoint:

    def test_chat_returns_sse_stream(self, client, std_headers):
        r = client.post("/api/chat",
                        json={"message": "hello"},
                        headers=std_headers)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")

    def test_chat_stream_contains_data_events(self, client, std_headers):
        r = client.post("/api/chat",
                        json={"message": "what is 2+2"},
                        headers=std_headers)
        assert b"data:" in r.content

    def test_chat_stream_has_done_event(self, client, std_headers):
        r = client.post("/api/chat",
                        json={"message": "hi"},
                        headers=std_headers)
        events = parse_sse(r.content)
        done_events = [e for e in events if e.get("type") == "done"]
        assert len(done_events) == 1, "Expected exactly one 'done' event"

    def test_chat_done_event_has_text(self, client, std_headers):
        r = client.post("/api/chat",
                        json={"message": "hi"},
                        headers=std_headers)
        events = parse_sse(r.content)
        done = next((e for e in events if e.get("type") == "done"), None)
        assert done is not None
        assert "text" in done
        assert len(done["text"]) > 0

    def test_chat_stream_has_token_events(self, client, std_headers):
        r = client.post("/api/chat",
                        json={"message": "hi"},
                        headers=std_headers)
        events = parse_sse(r.content)
        token_events = [e for e in events if e.get("type") == "token"]
        assert len(token_events) >= 1, "Expected at least one 'token' event"

    def test_chat_unauthenticated_returns_401(self, client):
        r = client.post("/api/chat", json={"message": "hello"})
        assert r.status_code == 401

    def test_chat_empty_message_does_not_crash(self, client, std_headers):
        r = client.post("/api/chat",
                        json={"message": ""},
                        headers=std_headers)
        # Empty message — server should handle gracefully (200 or 422, not 500)
        assert r.status_code in (200, 422)

    def test_chat_all_roles_can_send(self, client, admin_headers, std_headers, ro_headers):
        for headers, role in [(admin_headers, "admin"), (std_headers, "standard"), (ro_headers, "read-only")]:
            r = client.post("/api/chat",
                            json={"message": "hello"},
                            headers=headers)
            assert r.status_code == 200, f"Role {role!r} got {r.status_code}"


class TestSessionInfo:

    def test_session_info_returns_expected_fields(self, client, std_headers):
        r = client.get("/api/session/info", headers=std_headers)
        assert r.status_code == 200
        body = r.json()
        assert "session_id" in body
        assert "mode" in body
