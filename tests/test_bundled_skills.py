"""
The starter skills SUNI ships, and the two ways they go wrong quietly.

**A skill naming a tool that does not exist is a dead recipe.** It looks correct
in the admin panel, it is selected normally, and it fails at the point of use —
which is exactly the shape of the bug that had `network_tool` and `memory_tool`
registered in the CLI but not the server, and `create_scheduled_task` registered
nowhere. So every tool named in a SKILL.md is checked against the tool schemas
that actually exist.

**Skills are not free.** `SkillStore.level0_context()` injects every skill's name
and description as a system message on EVERY turn. With `num_ctx` at 8192 on the
default local model, the catalogue is a standing tax on the context window
before the user has said anything. The size assertions here exist so adding
skills stays a deliberate decision rather than a drift.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ROOT / "bundled_skills"

CATEGORIES = {"business", "content", "data", "development", "documents",
              "email", "knowledge", "media", "productivity", "research"}

# Backticked snake_case words in skill bodies that are parameters or field
# names, not tools. Listed explicitly so a typo'd TOOL name still fails.
NOT_TOOLS = {"attachment_paths", "tool_count", "num_ctx", "file_path"}


def _skill_files() -> list[Path]:
    return sorted(BUNDLED.rglob("SKILL.md"))


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path} has no frontmatter"
    _, fm, body = text.split("---", 2)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def _registered_tool_names() -> set[str]:
    """Every tool name the model can actually call."""
    sys.path.insert(0, str(ROOT))
    import suni.tools as T
    names: set[str] = set()
    for _, mod_name, _ in pkgutil.iter_modules(T.__path__):
        try:
            mod = importlib.import_module(f"suni.tools.{mod_name}")
        except Exception:
            continue
        for attr in dir(mod):
            v = getattr(mod, attr)
            if isinstance(v, dict) and "name" in v and "description" in v:
                names.add(v["name"])
    return names


# ── the dead-recipe guard ────────────────────────────────────────────────────
def test_there_are_tools_to_check_against():
    """If the discovery returns nothing the tool check below passes vacuously."""
    assert len(_registered_tool_names()) > 20


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_every_tool_a_skill_names_actually_exists(path):
    real = _registered_tool_names()
    _, body = _frontmatter(path)
    claimed = set(re.findall(r"`([a-z][a-z0-9_]{3,})`?\(?", body))
    unknown = sorted(c for c in claimed
                     if "_" in c and c not in real and c not in NOT_TOOLS)
    assert not unknown, (
        f"{path.parent.name} names tools that do not exist: {unknown}. "
        "A skill calling a missing tool fails at the point of use.")


# ── the per-turn budget ──────────────────────────────────────────────────────
@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_the_description_stays_short(path):
    """The description is injected on every request; the body is not."""
    meta, _ = _frontmatter(path)
    desc = meta.get("description", "")
    assert desc, f"{path} has no description"
    assert len(desc) <= 120, (
        f"{path.parent.name}: description is {len(desc)} chars. It is injected "
        "into every conversation — keep it under 120.")


def test_the_whole_catalogue_fits_a_sane_share_of_the_context():
    """Guards against skill drift. 8192 num_ctx on the default model; the
    catalogue should not creep toward a fifth of it."""
    total = 0
    for path in _skill_files():
        meta, _ = _frontmatter(path)
        total += len(meta.get("name", "")) + len(meta.get("description", "")) + 40
    approx_tokens = total // 4
    assert approx_tokens < 1200, (
        f"the bundled catalogue is ~{approx_tokens} tokens on every turn. "
        "Remove or merge skills before adding more.")


# ── frontmatter integrity ────────────────────────────────────────────────────
@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_frontmatter_is_complete_and_consistent(path):
    meta, body = _frontmatter(path)
    for field in ("name", "slug", "category", "description", "version", "tool_count"):
        assert field in meta, f"{path} is missing '{field}'"
    assert meta["slug"] == path.parent.name, "slug must match its directory"
    assert meta["category"] == path.parent.parent.name, "category must match its directory"
    assert meta["category"] in CATEGORIES, f"unknown category {meta['category']}"
    assert meta["version"].isdigit()
    assert meta["tool_count"].isdigit()
    assert body.strip(), "skill body is empty"


def test_slugs_are_unique():
    slugs = [p.parent.name for p in _skill_files()]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    assert not dupes, f"duplicate slugs: {dupes}"


# ── delivery to EXISTING instances ───────────────────────────────────────────
def test_new_skills_reach_an_instance_that_already_seeded(tmp_path):
    """Seeding is gated by a sentinel. Without bumping its version, a new
    bundled skill only ever appears on a fresh install — which is a feature
    that ships to nobody."""
    from suni import skills as sk

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # simulate an instance seeded under the PREVIOUS version
    (skills_dir / ".bundled_seeded_v2").write_text("seeded\n", encoding="utf-8")

    store = sk.SkillStore(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    seeded = {p.parent.name for p in skills_dir.rglob("SKILL.md")}
    for slug in ("write-a-skill", "status-report", "threat-model"):
        assert slug in seeded, (
            f"{slug} did not reach an already-seeded instance — bump _SEED_SENTINEL")
    assert store is not None


def test_seeding_never_overwrites_a_skill_the_user_edited(tmp_path):
    from suni import skills as sk

    skills_dir = tmp_path / "skills"
    (skills_dir / "development").mkdir(parents=True)
    edited = skills_dir / "development" / "threat-model"
    edited.mkdir()
    (edited / "SKILL.md").write_text("MY OWN VERSION", encoding="utf-8")

    sk.SkillStore(db_path=tmp_path / "skills.db", skills_dir=skills_dir)
    assert (edited / "SKILL.md").read_text(encoding="utf-8") == "MY OWN VERSION"


def test_the_sentinel_was_bumped_for_the_current_bundle():
    """The version in the sentinel name has to move when the bundle changes."""
    src = (ROOT / "suni" / "skills.py").read_text(encoding="utf-8")
    m = re.search(r'_SEED_SENTINEL\s*=\s*"\.bundled_seeded_v(\d+)"', src)
    assert m, "sentinel constant not found"
    assert int(m.group(1)) >= 3, "new bundled skills need a sentinel bump to ship"
