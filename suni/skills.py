"""
SUNI Skills System — Procedural Memory.

Skills are reusable SKILL.md documents that capture how SUNI solved complex tasks.
After any task requiring 5+ tool calls, SUNI auto-generates a skill capturing the
approach so it can execute the same class of task faster and more accurately next time.

Storage layout:
  memory/skills.db               — SQLite index with FTS5 full-text search
  memory/skills/{category}/{slug}/SKILL.md  — Markdown content with YAML frontmatter

YAML frontmatter fields:
  name, slug, category, description, version, created_at, tool_count, used_count, last_used
"""
from __future__ import annotations
import re
import sqlite3
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILLS_DIR = Path("memory/skills")
SKILLS_DB  = Path("memory/skills.db")
# Starter skills shipped with SUNI (tracked in the repo). Seeded once into the
# user's skills dir on first run; later edits/deletes are respected via a sentinel.
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "bundled_skills"
_SEED_SENTINEL     = ".bundled_seeded_v2"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_RE        = re.compile(r"[^\w\-]")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower().strip()).strip("-")[:64]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) from a SKILL.md string."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    import yaml
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    body = content[m.end():]
    return fm, body


def _build_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k in ("name", "slug", "category", "description", "version",
              "created_at", "tool_count", "used_count", "last_used"):
        if k in meta:
            lines.append(f"{k}: {meta[k]}")
    lines.append("---")
    return "\n".join(lines) + "\n"


class SkillStore:
    """
    Thread-safe skill store backed by SQLite LIKE search.
    All SKILL.md files live on disk; the DB is an index for fast lookup.
    LIKE search is fast enough for < 500 skills and has zero compatibility issues.
    """

    def __init__(self, db_path: str | Path = SKILLS_DB,
                 skills_dir: str | Path = SKILLS_DIR) -> None:
        self.db_path   = Path(db_path)
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._seed_bundled()
        self._sync_from_disk()

    def _seed_bundled(self) -> None:
        """Copy SUNI's shipped starter skills into the user's skills dir ONCE.
        A sentinel marks completion so a user who later edits/deletes a starter
        skill doesn't get it silently re-seeded on the next startup."""
        sentinel = self.skills_dir / _SEED_SENTINEL
        if sentinel.exists() or not BUNDLED_SKILLS_DIR.is_dir():
            return
        import shutil
        for skill_file in BUNDLED_SKILLS_DIR.rglob("SKILL.md"):
            try:
                rel = skill_file.relative_to(BUNDLED_SKILLS_DIR)
                dst = self.skills_dir / rel
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(skill_file, dst)
            except Exception:
                pass
        try:
            sentinel.write_text("seeded\n", encoding="utf-8")
        except Exception:
            pass

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    slug        TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    category    TEXT NOT NULL DEFAULT 'general',
                    description TEXT NOT NULL DEFAULT '',
                    version     INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL,
                    tool_count  INTEGER NOT NULL DEFAULT 0,
                    used_count  INTEGER NOT NULL DEFAULT 0,
                    last_used   TEXT,
                    file_path   TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS skills_body (
                    slug TEXT PRIMARY KEY,
                    body TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_skills_used ON skills(used_count DESC)")
            con.commit()

    def _sync_from_disk(self) -> None:
        """Import any SKILL.md files on disk that aren't yet in the DB."""
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                fm, body = _parse_frontmatter(content)
                slug = fm.get("slug") or _slugify(fm.get("name", skill_file.parent.name))
                with self._connect() as con:
                    existing = con.execute(
                        "SELECT slug FROM skills WHERE slug=?", (slug,)
                    ).fetchone()
                    if not existing:
                        self._upsert(con, fm, body, str(skill_file))
            except Exception:
                pass

    def _upsert(self, con: sqlite3.Connection, fm: dict, body: str,
                file_path: str) -> None:
        slug = fm.get("slug", _slugify(fm.get("name", "skill")))
        con.execute("""
            INSERT INTO skills (slug, name, category, description, version,
                                created_at, tool_count, used_count, last_used, file_path)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name, category=excluded.category,
                description=excluded.description, version=excluded.version,
                tool_count=excluded.tool_count, used_count=excluded.used_count,
                last_used=excluded.last_used, file_path=excluded.file_path
        """, (
            slug,
            fm.get("name", slug),
            fm.get("category", "general"),
            fm.get("description", ""),
            fm.get("version", 1),
            fm.get("created_at", _now_iso()),
            fm.get("tool_count", 0),
            fm.get("used_count", 0),
            fm.get("last_used"),
            file_path,
        ))
        con.execute("""
            INSERT INTO skills_body (slug, body) VALUES (?,?)
            ON CONFLICT(slug) DO UPDATE SET body=excluded.body
        """, (slug, body))
        # (No separate FTS table — search uses LIKE on skills + skills_body)

    # ── Public API ────────────────────────────────────────────────────────────

    def list(self, category: str = "") -> list[dict]:
        """Return Level-0 metadata (no body) for all skills."""
        with self._connect() as con:
            if category:
                rows = con.execute(
                    "SELECT * FROM skills WHERE category=? ORDER BY used_count DESC",
                    (category,)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM skills ORDER BY used_count DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """LIKE search over name + description + body. Returns Level-0 metadata."""
        pat = f"%{query}%"
        with self._connect() as con:
            rows = con.execute("""
                SELECT DISTINCT s.*
                FROM skills s
                LEFT JOIN skills_body b USING(slug)
                WHERE s.name LIKE ? OR s.description LIKE ? OR b.body LIKE ?
                ORDER BY s.used_count DESC
                LIMIT ?
            """, (pat, pat, pat, top_k)).fetchall()
        return [dict(r) for r in rows]

    def get(self, slug: str) -> dict | None:
        """Return full skill including body. Increments used_count."""
        with self._connect() as con:
            row = con.execute("SELECT * FROM skills WHERE slug=?", (slug,)).fetchone()
            if not row:
                return None
            body_row = con.execute(
                "SELECT body FROM skills_body WHERE slug=?", (slug,)
            ).fetchone()
            con.execute(
                "UPDATE skills SET used_count=used_count+1, last_used=? WHERE slug=?",
                (_now_iso(), slug)
            )
            con.commit()
        result = dict(row)
        result["body"] = body_row["body"] if body_row else ""
        return result

    def save(self, name: str, body: str, category: str = "general",
             description: str = "", tool_count: int = 0,
             slug: str = "") -> str:
        """Create or update a skill. Returns the slug."""
        slug = slug or _slugify(name)
        dest_dir  = self.skills_dir / category / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / "SKILL.md"

        # Load existing row to preserve used_count and increment version
        with self._connect() as con:
            existing = con.execute(
                "SELECT version, used_count FROM skills WHERE slug=?", (slug,)
            ).fetchone()
        version    = (existing["version"] + 1) if existing else 1
        used_count = existing["used_count"] if existing else 0

        fm = {
            "name":        name,
            "slug":        slug,
            "category":    category,
            "description": description,
            "version":     version,
            "created_at":  _now_iso(),
            "tool_count":  tool_count,
            "used_count":  used_count,
            "last_used":   None,
        }

        content = _build_frontmatter(fm) + "\n" + body.lstrip()
        dest_file.write_text(content, encoding="utf-8")

        with self._connect() as con:
            self._upsert(con, fm, body, str(dest_file))
            con.commit()

        return slug

    def delete(self, slug: str) -> bool:
        """Delete a skill from DB and disk. Returns True if it existed."""
        with self._connect() as con:
            full_row = con.execute(
                "SELECT rowid, * FROM skills WHERE slug=?", (slug,)
            ).fetchone()
            if not full_row:
                return False
            con.execute("DELETE FROM skills WHERE slug=?", (slug,))
            con.execute("DELETE FROM skills_body WHERE slug=?", (slug,))
            con.commit()
        file_path = full_row["file_path"]
        # Remove file
        try:
            p = Path(file_path)
            if p.exists():
                p.unlink()
            parent = p.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass
        return True

    def stats(self) -> dict:
        with self._connect() as con:
            total    = con.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            cats_raw = con.execute(
                "SELECT category, COUNT(*) c FROM skills GROUP BY category ORDER BY c DESC"
            ).fetchall()
            top      = con.execute(
                "SELECT slug, name, used_count FROM skills ORDER BY used_count DESC LIMIT 5"
            ).fetchall()
        return {
            "total":      total,
            "categories": {r["category"]: r["c"] for r in cats_raw},
            "top_used":   [dict(r) for r in top],
        }

    def level0_context(self) -> str:
        """Return a compact Level-0 skills list for system prompt injection.

        Format: one line per skill  — [SKILL:slug] name (category): description
        Kept under ~2k tokens for 50 skills.
        """
        skills = self.list()
        if not skills:
            return ""
        lines = ["--- Available skills (use skill_view to load full content) ---"]
        for s in skills:
            desc = textwrap.shorten(s.get("description", ""), width=80, placeholder="…")
            lines.append(f"[SKILL:{s['slug']}] {s['name']} ({s['category']}): {desc}")
        lines.append("--- End skills ---")
        return "\n".join(lines)
