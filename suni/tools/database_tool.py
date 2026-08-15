"""
NL → SQL database tools for SUNI.

Two tools:
  db_schema  — introspect table names and columns (read-only, no approval)
  db_query   — execute a SELECT query           (read-only, no approval)
  db_execute — execute a mutating query         (approval gate via _CONSEQUENTIAL)

The model generates SQL itself after calling db_schema; db_execute routes
through the central approval gate for INSERT/UPDATE/DELETE/DDL.

Connection string priority:
  1. suni_config["db_connection_string"]
  2. ENV var  SUNI_DB_CONN
  3. Fallback to the SUNIverse SQL Server used by articles.py
"""
from __future__ import annotations
import os
import re
import textwrap
from typing import Any

# No hardcoded fallback connection string: it would embed a live password in
# source. The SUNIverse default is assembled from SUNIVERSE_DB_* in the
# environment by ingestion/articles.py, which is the single place those
# credentials are read.

_MAX_ROWS   = 200   # hard cap for SELECT results
_MAX_CELL   = 200   # truncate long cell values
_COL_WIDTH  = 24    # column header width in text output


def _conn_str() -> str:
    try:
        from .. import config as _cfg
        cs = _cfg.get("db_connection_string", "")
        if cs:
            return cs
    except Exception:
        pass
    cs = os.environ.get("SUNI_DB_CONN")
    if cs:
        return cs
    # Fall back to the SUNIverse connection, assembled from the environment.
    from ..ingestion.articles import _conn_str as _suniverse_conn_str
    return _suniverse_conn_str()


def _connect():
    import pyodbc
    return pyodbc.connect(_conn_str(), timeout=10)


def _is_select(sql: str) -> bool:
    return bool(re.match(r'\s*SELECT\b', sql.strip(), re.IGNORECASE))


def _fmt_rows(columns: list[str], rows: list[tuple]) -> str:
    """Format query results as an aligned text table."""
    col_w = [min(max(len(c), min(len(str(r[i])), _MAX_CELL) if rows else 0), _COL_WIDTH)
             for i, c in enumerate(columns)]
    header = "  ".join(c[:w].ljust(w) for c, w in zip(columns, col_w))
    sep    = "  ".join("-" * w for w in col_w)
    lines  = [header, sep]
    for row in rows:
        cells = [str(v)[:_MAX_CELL] if v is not None else "NULL" for v in row]
        lines.append("  ".join(c.ljust(w) for c, w in zip(cells, col_w)))
    return "\n".join(lines)


# ── Schemas ──────────────────────────────────────────────────────────────────

SCHEMA_SCHEMA = {
    "name": "db_schema",
    "description": (
        "Retrieve database schema: list all tables or show columns for a specific table. "
        "Use this before writing SQL so you know the exact column names and types."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "Specific table to describe. Leave empty to list all tables.",
                "default": "",
            },
        },
        "required": [],
    },
}

QUERY_SCHEMA = {
    "name": "db_query",
    "description": (
        "Execute a read-only SELECT query against the database and return results. "
        "Always use db_schema first to verify table and column names. "
        "Do NOT use for INSERT, UPDATE, DELETE, or DDL — use db_execute for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql":    {"type": "string", "description": "The SELECT SQL query to execute"},
            "params": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional positional parameters (? placeholders)",
                "default": [],
            },
        },
        "required": ["sql"],
    },
}

EXECUTE_SCHEMA = {
    "name": "db_execute",
    "description": (
        "Execute a mutating SQL statement (INSERT, UPDATE, DELETE, or DDL). "
        "The user will be asked to approve this before it runs. "
        "Always show the exact SQL in your message before calling this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql":    {"type": "string", "description": "The SQL statement to execute"},
            "params": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional positional parameters",
                "default": [],
            },
        },
        "required": ["sql"],
    },
}


# ── Handlers ─────────────────────────────────────────────────────────────────

def schema_handler(table_name: str = "") -> str:
    try:
        conn = _connect()
        cur  = conn.cursor()
        if not table_name:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
            )
            tables = [row[0] for row in cur.fetchall()]
            conn.close()
            return "Tables:\n" + "\n".join(f"  {t}" for t in tables)
        else:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? "
                "ORDER BY ORDINAL_POSITION",
                (table_name,),
            )
            rows = cur.fetchall()
            conn.close()
            if not rows:
                return f"Table '{table_name}' not found or has no columns."
            lines = [f"Table: {table_name}", "-" * 40]
            for col, dtype, max_len, nullable in rows:
                type_str = dtype + (f"({max_len})" if max_len else "")
                null_str = "NULL" if nullable == "YES" else "NOT NULL"
                lines.append(f"  {col:<30} {type_str:<20} {null_str}")
            return "\n".join(lines)
    except Exception as e:
        return f"Schema error: {e}"


def query_handler(sql: str, params: list | None = None) -> str:
    if not _is_select(sql):
        return (
            "Error: db_query only supports SELECT statements. "
            "Use db_execute for INSERT/UPDATE/DELETE."
        )
    try:
        conn = _connect()
        cur  = conn.cursor()
        cur.execute(sql, params or [])
        columns = [d[0] for d in cur.description] if cur.description else []
        rows    = cur.fetchmany(_MAX_ROWS)
        total   = len(rows)
        conn.close()
        if not rows:
            return "Query returned no rows."
        result = _fmt_rows(columns, rows)
        if total == _MAX_ROWS:
            result += f"\n\n(showing first {_MAX_ROWS} rows — add WHERE/TOP to narrow results)"
        return result
    except Exception as e:
        return f"Query error: {e}"


def execute_handler(sql: str, params: list | None = None) -> str:
    if _is_select(sql):
        return "Use db_query for SELECT statements — db_execute is for mutating queries only."
    try:
        conn = _connect()
        cur  = conn.cursor()
        cur.execute(sql, params or [])
        affected = cur.rowcount
        conn.commit()
        conn.close()
        return f"Executed successfully. Rows affected: {affected}."
    except Exception as e:
        return f"Execute error: {e}"
