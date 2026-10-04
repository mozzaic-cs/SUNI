"""A coding run is approved by a signed-in person. The service token and
per-user API tokens can open /api/chat AND answer approvals, so without this
a script could start a run that edits code and approve it itself."""
import asyncio

from suni import approval


def _pending(user_id: str, tool: str) -> str:
    aid = f"t{abs(hash((user_id, tool))) % 10**8}"
    fut = asyncio.new_event_loop().create_future()
    approval._pending[aid] = {"user_id": user_id, "future": fut, "tool": tool,
                              "args": {}, "summary": tool, "preview": None, "risk": None,
                              "created": ""}
    return aid


def test_the_service_token_cannot_approve_a_coding_run(client):
    from suni.web import server
    from suni.web.server import _SERVICE_USER
    aid = _pending(_SERVICE_USER["id"], "code_task")
    try:
        r = client.post(f"/api/approval/{aid}", json={"decision": "allow", "tool": "code_task"},
                        headers={"Authorization": f"Bearer {server._API_TOKEN}"})
        assert r.status_code == 403
        assert aid in approval._pending            # still waiting for a person
    finally:
        approval._pending.pop(aid, None)


def test_the_service_token_can_still_deny_one(client):
    from suni.web import server
    from suni.web.server import _SERVICE_USER
    aid = _pending(_SERVICE_USER["id"], "code_task")
    try:
        r = client.post(f"/api/approval/{aid}", json={"decision": "deny", "tool": "code_task"},
                        headers={"Authorization": f"Bearer {server._API_TOKEN}"})
        assert r.status_code == 200
    finally:
        approval._pending.pop(aid, None)


def test_a_signed_in_person_can_approve_it(client, admin_headers, test_users):
    uid = test_users["admin"]["id"] if isinstance(test_users["admin"], dict) else test_users["admin"]
    aid = _pending(uid, "code_task")
    try:
        r = client.post(f"/api/approval/{aid}", json={"decision": "allow", "tool": "code_task"},
                        headers=admin_headers)
        assert r.status_code == 200
    finally:
        approval._pending.pop(aid, None)


def test_other_tools_keep_their_token_behaviour(client):
    from suni.web import server
    from suni.web.server import _SERVICE_USER
    aid = _pending(_SERVICE_USER["id"], "send_email")
    try:
        r = client.post(f"/api/approval/{aid}", json={"decision": "allow", "tool": "send_email"},
                        headers={"Authorization": f"Bearer {server._API_TOKEN}"})
        assert r.status_code == 200
    finally:
        approval._pending.pop(aid, None)
