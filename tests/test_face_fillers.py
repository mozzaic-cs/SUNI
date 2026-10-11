"""Spoken fillers must fit what is happening.

Measured in the log: plain chat turns ("Olá Suni.") take 7-11s through Claude
Code, and the first filler fired at 6s on every one of them with a task phrase
("Um momento, vou ver isso.") — after a greeting.
"""
import pathlib
import re

FACE = (pathlib.Path(__file__).resolve().parent.parent
        / "suni/web/face.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = FACE.index(f"function {name}(")
    return FACE[i:FACE.index("\n}\n", i)]


def test_a_plain_chat_turn_gets_no_early_filler():
    tick = _fn("_fillerTick")
    assert "_fill.n === 0 && !_fill.kind" in tick, "6s filler fires with no work started"
    plain = int(re.search(r"_FILL_PLAIN_MS = (\d+)", FACE).group(1))
    assert plain >= 12000, "would still talk over an ordinary 7-11s chat reply"
    assert "_fill.t0 = performance.now()" in _fn("_fillerStart")


def test_the_opening_line_assumes_no_task():
    ack = re.search(r"pt: \{\s*ack:\s*\[([^\]]*)\]", FACE).group(1)
    for task_words in ("ver isso", "tratar disso", "analisar"):
        assert task_words not in ack
