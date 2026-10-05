"""/api/tools/catalog feeds the role matrix: each native tool's own
description and the approval gate's risk call, read live so the page never
carries a copy that can drift."""


def test_admin_gets_descriptions_and_risk(client, admin_headers):
    r = client.get("/api/tools/catalog", headers=admin_headers)
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()}
    assert "run_shell" in tools and "read_file" in tools
    assert tools["run_shell"]["consequential"] is True
    assert tools["web_search"]["consequential"] is False
    assert tools["code_task"]["every_time"] is True
    assert all(t["description"] for t in tools.values() if t["name"] in ("run_shell", "read_file"))


def test_mcp_tools_are_not_listed(client, admin_headers):
    names = [t["name"] for t in client.get("/api/tools/catalog", headers=admin_headers).json()]
    from suni.web import server  # noqa: F401  (the app fixture built the registry)
    assert not any(n.startswith(("playwright_", "filesystem_")) for n in names)


def test_standard_user_is_refused(client, std_headers):
    assert client.get("/api/tools/catalog", headers=std_headers).status_code == 403


def test_the_matrix_learns_the_hard_floor(client, admin_headers):
    tools = {t["name"]: t for t in client.get("/api/tools/catalog", headers=admin_headers).json()}
    assert set(tools["code_task"]["never_roles"]) == {"standard", "read-only"}
    assert tools["run_shell"]["never_roles"] == []
