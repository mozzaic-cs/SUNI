"""
The Face interface must be able to answer an approval, not just watch.

From live use. Asked in Portuguese to make a PDF about Coimbra and email it,
SUNI did the whole job correctly:

    [TOOL] create_pdf ... PDF created
    [APPROVAL] requesting send_email(to=…, subject=Information about Coimbra)
    [APPROVAL] timed out
    [APPROVAL] denied
    [DONE] total=316.35s route=pdf tools=[create_pdf, send_email]

It researched, produced the PDF, chained to send_email, and asked before
sending — exactly as designed. But the request was made over the SSE stream and
`face.html` had no handler for `approval_request`, so the question was never
shown. The user watched the face "think" for five minutes while the server
waited for an answer that could not be given, then auto-denied.

Every gated tool was affected on that interface — send_email, write_file,
delete_file, run_shell, claude_task, db_execute, calendar, schedules. The
approval design was working; the surface it spoke through was missing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FACE = (ROOT / "suni" / "web" / "face.html").read_text(encoding="utf-8")
UI = (ROOT / "suni" / "web" / "ui.html").read_text(encoding="utf-8")
SHARED = (ROOT / "suni" / "web" / "approval_ui.js").read_text(encoding="utf-8")
CHAT = (ROOT / "suni" / "web" / "chat.html").read_text(encoding="utf-8")
I18N = (ROOT / "suni" / "web" / "i18n.js").read_text(encoding="utf-8")
SURFACES = {"ui.html": UI, "face.html": FACE, "chat.html": CHAT}

# Tools that require approval — from suni/approval.py. Each is unusable on any
# interface that cannot render the request.
GATED_SAMPLE = ["send_email", "write_file", "run_shell", "claude_task"]


@pytest.mark.parametrize("name", ["ui.html", "face.html", "chat.html"])
def test_every_surface_handles_the_approval_event(name):
    """All three chat interfaces, not just the one that happened to have it.
    An interface that cannot render the request cannot complete any gated
    action at all."""
    assert "approval_request" in SURFACES[name], (
        f"{name} ignores approval_request; every gated tool will time out "
        "there while the user watches 'thinking'")


@pytest.mark.parametrize("name", ["ui.html", "face.html", "chat.html"])
def test_every_surface_can_send_a_decision(name):
    """Rendering the question is half of it — the answer has to reach the
    endpoint that releases the blocked request."""
    src = SURFACES[name]
    if "showApprovalPrompt" in src:
        src = src + SHARED          # the two voice surfaces share one module
    assert "/api/approval/" in src
    assert "allow" in src and "deny" in src


def test_the_two_voice_surfaces_share_one_implementation():
    """Written twice, they drift; the second copy is the one that gets missed
    when a bug is fixed."""
    assert "showApprovalPrompt" in UI and "showApprovalPrompt" in FACE
    assert UI.count("function showApprovalPrompt") == 0
    assert FACE.count("function showApprovalPrompt") == 0
    assert "g.showApprovalPrompt" in SHARED


@pytest.mark.parametrize("name", ["ui.html", "face.html"])
def test_the_shared_module_is_actually_loaded(name):
    """A call to a function the page never loads is a ReferenceError that
    swallows the only prompt the user gets."""
    assert '/approval_ui.js' in SURFACES[name], f"{name} never loads the module"


def test_the_module_is_served():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert '@app.get("/approval_ui.js")' in srv


def test_a_missing_translation_falls_back_to_english_not_a_raw_key():
    """t() returns the key itself when a key is absent, so `t(k) || fallback`
    never fires — the button would read 'common.allow'."""
    assert "s === key" in SHARED, "no guard against rendering the raw key"


def test_the_prompt_does_not_depend_on_page_level_helpers():
    """`esc` is declared inside another block in these pages; the module
    carries its own."""
    assert "function esc(" in SHARED


@pytest.mark.parametrize("name", ["ui.html", "face.html"])
def test_the_surface_stops_looking_busy_while_it_waits(name):
    """The reported symptom was an interface that looked busy while it was in
    fact blocked on the user."""
    src = SURFACES[name]
    i = src.index("showApprovalPrompt")
    assert "onWaiting" in src[i:i + 400]
    assert "setState('idle')" in src[i:i + 400]


def test_the_gated_tools_are_still_gated():
    """This fixes the surface, not the policy. If these stopped requiring
    approval the symptom would also disappear — for the wrong reason."""
    from suni.approval import _CONSEQUENTIAL  # noqa: PLC0415
    for tool in GATED_SAMPLE:
        assert tool in _CONSEQUENTIAL, f"{tool} is no longer gated"


def test_the_timeout_is_long_enough_to_answer_but_not_to_strand():
    """Five minutes of an unanswerable question is what made this look like a
    hang. The window only makes sense once the prompt is actually visible."""
    from suni.approval import APPROVAL_TIMEOUT
    assert 60 <= APPROVAL_TIMEOUT <= 600


# ── standing permission ──────────────────────────────────────────────────────
def test_every_surface_offers_always_allow():
    """The gate fires even when the intent judge rules the call on-intent —
    an explicitly requested email still carries a model-composed body and
    attachment. That is the right default, so the way OUT of being asked every
    time has to exist on every surface, not just chat.html."""
    assert "always_allow" in CHAT
    assert "always_allow" in SHARED, \
        "the voice surfaces cannot grant standing permission"


def test_always_allow_is_only_sent_with_an_allow():
    """'Always deny' is not stored server-side; sending it would be a no-op the
    user believes took effect."""
    i = SHARED.index("var always =")
    assert "decision === 'allow'" in SHARED[i:i + 160]


def test_the_always_allow_label_is_translated():
    found = len(re.findall(r"['\"]chat\.always_allow['\"]\s*:", I18N))
    assert found == 2, "chat.always_allow is not defined in both en and pt"


def test_the_server_still_honours_the_flag():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('always = bool(body.get("always_allow"')
    assert "add_trust_rule" in srv[i:i + 700], \
        "always_allow is accepted but no trust rule is stored"


# ── taking it back ───────────────────────────────────────────────────────────
# Standing permissions now survive a restart (see test_trust_rules.py). That
# makes revocation part of the feature rather than a nicety: GET and DELETE
# /api/approval/trust had existed for months with no interface on top of them,
# so a rule could be granted from any surface and withdrawn from none.

@pytest.mark.parametrize("name", ["ui.html", "face.html", "chat.html"])
def test_every_surface_can_revoke_a_standing_permission(name):
    """Granting is offered on all three, so withdrawing has to be too."""
    assert "renderTrustRules" in SURFACES[name], \
        f"{name} can grant standing permission but never shows it again"


@pytest.mark.parametrize("name", ["ui.html", "face.html", "chat.html"])
def test_the_revoke_list_has_somewhere_to_render(name):
    """The call is harmless without the container — and silently does nothing,
    which is the failure this whole file exists to catch."""
    assert 'id="trust-rules"' in SURFACES[name], f"{name} has no mount point"


def test_revoking_calls_the_delete_endpoint():
    i = SHARED.index("renderTrustRules")
    body = SHARED[i:]
    assert "'/api/approval/trust/'" in body
    assert "method: 'DELETE'" in body


def test_the_tool_name_is_escaped_into_the_url():
    """Tool names reach here from the server; encodeURIComponent keeps one
    containing a slash from addressing a different endpoint."""
    i = SHARED.index("'/api/approval/trust/'")
    assert "encodeURIComponent" in SHARED[i:i + 120]


def test_the_list_is_re_read_after_a_revoke():
    """Removing the row locally would show a revocation that failed server-side
    as though it had worked."""
    i = SHARED.index("method: 'DELETE'")
    assert "renderTrustRules(el)" in SHARED[i:i + 300]


@pytest.mark.parametrize("key", ["settings.trust_title", "settings.trust_hint",
                                 "common.revoke"])
def test_the_revoke_ui_is_translated(key):
    found = len(re.findall(r"['\"]%s['\"]\s*:" % re.escape(key), I18N))
    assert found == 2, f"{key} is not defined in both en and pt"
