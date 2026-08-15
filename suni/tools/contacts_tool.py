"""Orchestrator tools for the CRM-lite contact store."""
from __future__ import annotations
from .. import contacts as _contacts

SEARCH_SCHEMA = {
    "name": "contacts_search",
    "description": (
        "Search contacts by name, email, company, or notes. "
        "Returns a formatted list of matches with IDs. "
        "Use before sending emails or referencing people."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Name, email, company, or keyword to search for"},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "contacts_add",
    "description": "Add a new contact to the CRM. Returns the created contact with its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "name":    {"type": "string", "description": "Full name (required)"},
            "company": {"type": "string", "description": "Company or organisation"},
            "email":   {"type": "string", "description": "Email address"},
            "phone":   {"type": "string", "description": "Phone number"},
            "notes":   {"type": "string", "description": "Initial notes about this contact"},
            "tags":    {
                "type": "array",
                "items": {"type": "string"},
                "description": "Labels e.g. ['client', 'hot-lead', 'supplier']",
            },
        },
        "required": ["name"],
    },
}

UPDATE_SCHEMA = {
    "name": "contacts_update",
    "description": "Update an existing contact by ID. Only provided fields are changed.",
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string", "description": "Contact ID (8-char from contacts_search)"},
            "name":       {"type": "string"},
            "company":    {"type": "string"},
            "email":      {"type": "string"},
            "phone":      {"type": "string"},
            "notes":      {"type": "string", "description": "Replaces existing notes — use contacts_note to append"},
            "tags":       {"type": "array", "items": {"type": "string"}},
        },
        "required": ["contact_id"],
    },
}

NOTE_SCHEMA = {
    "name": "contacts_note",
    "description": (
        "Append a timestamped interaction note to a contact. "
        "Use after calls, meetings, emails, or any notable interaction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string", "description": "Contact ID"},
            "note":       {"type": "string", "description": "Note to append (will be timestamped automatically)"},
        },
        "required": ["contact_id", "note"],
    },
}


def _fmt(c: dict) -> str:
    parts = [f"[{c['id']}] {c['name']}"]
    if c.get("company"):
        parts.append(f"@ {c['company']}")
    if c.get("email"):
        parts.append(f"<{c['email']}>")
    if c.get("phone"):
        parts.append(c["phone"])
    tags = c.get("tags") or []
    if tags:
        parts.append(f"[{', '.join(tags)}]")
    if c.get("last_seen"):
        parts.append(f"last seen {c['last_seen'][:10]}")
    return " ".join(parts)


def search_handler(query: str) -> str:
    results = _contacts.search_contacts(query)
    if not results:
        return f"No contacts found for '{query}'."
    lines = [_fmt(c) for c in results]
    if any(c.get("notes") for c in results):
        lines_with_notes = []
        for c in results:
            lines_with_notes.append(_fmt(c))
            if c.get("notes"):
                for note_line in c["notes"].splitlines()[-3:]:  # last 3 note lines
                    lines_with_notes.append(f"  {note_line}")
        return "\n".join(lines_with_notes)
    return "\n".join(lines)


def add_handler(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    notes: str = "",
    tags: list[str] | None = None,
) -> str:
    result = _contacts.add_contact(name, company, email, phone, notes, tags)
    return f"Contact created: {_fmt(result)}"


def update_handler(
    contact_id: str,
    name: str | None = None,
    company: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> str:
    result = _contacts.update_contact(contact_id, name, company, email, phone, notes, tags)
    if not result:
        return f"Contact '{contact_id}' not found."
    return f"Contact updated: {_fmt(result)}"


def note_handler(contact_id: str, note: str) -> str:
    result = _contacts.append_note(contact_id, note)
    if not result:
        return f"Contact '{contact_id}' not found."
    return f"Note added to {_fmt(result)}."
