from __future__ import annotations
from pathlib import Path

READ_SCHEMA = {
    "name": "read_file",
    "description": "Read the full text contents of a file.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
        },
        "required": ["path"],
    },
}

WRITE_SCHEMA = {
    "name": "write_file",
    "description": "Write text content to a file, creating directories as needed.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Text content to write"},
        },
        "required": ["path", "content"],
    },
}

LIST_SCHEMA = {
    "name": "list_files",
    "description": "List files in a directory, optionally filtered by a glob pattern.",
    "parameters": {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Directory to list", "default": "."},
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py')", "default": "*"},
        },
    },
}


def read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading '{path}': {e}"


def write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing '{path}': {e}"


def list_files(directory: str = ".", pattern: str = "*") -> str:
    try:
        files = sorted(Path(directory).glob(pattern))
        if not files:
            return "(no files matched)"
        return "\n".join(str(f) for f in files)
    except Exception as e:
        return f"Error listing '{directory}': {e}"
