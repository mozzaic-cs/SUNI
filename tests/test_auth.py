"""
Authentication tests.

Covers:
  - Login (success / bad credentials / unknown user)
  - JWT verification (valid / invalid / malformed)
  - /api/auth/me
  - /api/auth/logout
  - /api/auth/first-run
  - Role-based access to admin endpoints
"""
import pytest


class TestLogin:

    def test_admin_login_returns_tokens(self, client, test_users):
        r = client.post("/api/auth/login",
                        data={"username": "admin_test", "password": "Admin123!"})  # allowlist-secret: test fixture
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["role"] == "admin"

    def test_standard_login_returns_correct_role(self, client):
        r = client.post("/api/auth/login",
                        data={"username": "std_test", "password": "Standard123!"})  # allowlist-secret: test fixture
        assert r.status_code == 200
        assert r.json()["role"] == "standard"

    def test_readonly_login_returns_correct_role(self, client):
        r = client.post("/api/auth/login",
                        data={"username": "ro_test", "password": "Readonly123!"})  # allowlist-secret: test fixture
        assert r.status_code == 200
        assert r.json()["role"] == "read-only"

    def test_wrong_password_returns_401(self, client):
        r = client.post("/api/auth/login",
                        data={"username": "admin_test", "password": "wrongpassword"})  # allowlist-secret: test fixture
        assert r.status_code == 401

    def test_unknown_user_returns_401(self, client):
        r = client.post("/api/auth/login",
                        data={"username": "nobody", "password": "anything"})  # allowlist-secret: test fixture
        assert r.status_code == 401

    def test_empty_credentials_returns_401(self, client):
        r = client.post("/api/auth/login", data={"username": "", "password": ""})  # allowlist-secret: test fixture
        assert r.status_code == 401

    def test_login_response_has_no_password(self, client):
        r = client.post("/api/auth/login",
                        data={"username": "admin_test", "password": "Admin123!"})  # allowlist-secret: test fixture
        body = r.json()
        assert "password" not in body
        assert "password_h" not in body


class TestAuthMe:

    def test_me_with_valid_token(self, client, admin_headers):
        r = client.get("/api/auth/me", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "admin_test"
        assert body["role"] == "admin"

    def test_me_without_token_returns_401(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_me_with_invalid_token_returns_401(self, client):
        r = client.get("/api/auth/me",
                       headers={"Authorization": "Bearer this.is.garbage"})
        assert r.status_code == 401

    def test_me_with_malformed_header_returns_401(self, client):
        r = client.get("/api/auth/me",
                       headers={"Authorization": "NotBearer sometoken"})
        assert r.status_code == 401

    def test_me_with_empty_bearer_returns_401(self, client):
        r = client.get("/api/auth/me",
                       headers={"Authorization": "Bearer "})
        assert r.status_code == 401

    def test_me_std_user_correct_role(self, client, std_headers):
        r = client.get("/api/auth/me", headers=std_headers)
        assert r.status_code == 200
        assert r.json()["role"] == "standard"

    def test_me_readonly_user_correct_role(self, client, ro_headers):
        r = client.get("/api/auth/me", headers=ro_headers)
        assert r.status_code == 200
        assert r.json()["role"] == "read-only"


class TestLogout:

    def test_logout_returns_ok(self, client):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestFirstRun:

    def test_first_run_returns_bool(self, client):
        r = client.get("/api/auth/first-run")
        assert r.status_code == 200
        # Users were already created, so first_run should be False
        assert r.json()["first_run"] is False


class TestAdminEndpointRoleGating:

    def test_admin_can_list_users(self, client, admin_headers):
        r = client.get("/api/users", headers=admin_headers)
        assert r.status_code == 200
        users = r.json()
        assert isinstance(users, list)
        assert any(u["username"] == "admin_test" for u in users)

    def test_standard_cannot_list_users(self, client, std_headers):
        r = client.get("/api/users", headers=std_headers)
        assert r.status_code == 403

    def test_readonly_cannot_list_users(self, client, ro_headers):
        r = client.get("/api/users", headers=ro_headers)
        assert r.status_code == 403

    def test_unauthenticated_cannot_list_users(self, client):
        r = client.get("/api/users")
        assert r.status_code == 401

    def test_all_auth_users_can_get_config(self, client, admin_headers, std_headers, ro_headers):
        for headers in (admin_headers, std_headers, ro_headers):
            r = client.get("/api/config", headers=headers)
            assert r.status_code == 200

    def test_unauthenticated_cannot_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 401
