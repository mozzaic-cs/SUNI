"""Claude Code tools that change things go through the approval gate.

The role matrix showed claude_schedule_task and claude_init_project with no
risk tag: they had never been gated. One gives Claude Code bare Bash and
leaves a scheduled task running unattended; the other a bare Write, which
(measured) writes outside any folder. The read-only advisor stays ungated.
"""
from suni import approval


def test_the_two_that_change_things_are_gated():
    assert approval.is_consequential("claude_schedule_task")
    assert approval.is_consequential("claude_init_project")


def test_the_card_says_what_would_happen():
    p = approval.build_preview("claude_schedule_task",
                               {"task_name": "nightly", "schedule": "daily at 2am", "command": "backup.bat"})
    assert "unattended" in p and "backup.bat" in p and "daily at 2am" in p
    assert "C:/proj" in approval.build_preview("claude_init_project", {"directory": "C:/proj"})


def test_the_read_only_advisor_is_not():
    assert not approval.is_consequential("claude_advisor")
