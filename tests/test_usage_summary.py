"""The Usage page's tables must count the same requests.

Totals and by-user counted only rows that carried token counts, while
by-mode counted every chat turn, so the same 30 days read 54 requests in one
place and 79 in another. Claude Code turns long recorded no tokens; they
were real requests all the same.
"""
from suni import audit


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_DB", tmp_path / "audit.db")
    audit.init_db()


def test_every_table_counts_the_same_chat_requests(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.log(user_id="u1", username="ana", route="chat", mode="assistant",
              prompt_tokens=100, gen_tokens=20)
    audit.log(user_id="u1", username="ana", route="chat", mode="collaborate")   # no tokens
    audit.log(user_id="u1", username="ana", route="agent.created",
              prompt_tokens=50, gen_tokens=5)                                  # not a chat turn

    u = audit.usage_summary(days=30)
    assert u["totals"]["requests"] == 2
    assert u["totals"]["with_tokens"] == 1
    assert sum(m["requests"] for m in u["by_mode"]) == u["totals"]["requests"]
    assert sum(r["requests"] for r in u["by_user"]) == u["totals"]["requests"]
    assert u["totals"]["prompt_tokens"] == 100 and u["totals"]["gen_tokens"] == 20
    assert {m["mode"] for m in u["by_mode"]} == {"assistant", "collaborate"}


def test_tools_called_round_trips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.log(user_id="u1", username="ana", route="chat",
              tools_called=["web_search", "read_file"])
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "audit.db"))
    got = c.execute("SELECT tools_called FROM audit_log WHERE route='chat'").fetchone()[0]
    assert got == "web_search,read_file"
