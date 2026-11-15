"""Shared type aliases, so function signatures say which kind of record they take or return.

Rows come back from SQLite as plain dicts (decrypted by db.q), so these are aliases rather than classes:
they document intent without changing how data flows through the app.
"""
from __future__ import annotations

from typing import Any

Row = dict[str, Any]
"""One database row after decryption: column name -> value."""

Actor = dict[str, Any]
"""The signed-in person, as returned by auth.current(): id, family_id, role, name, email, lang, stage, ..."""

Member = dict[str, Any]
"""A member row, or the parts of one that an email needs: id, family_id, name, email."""

Suggestion = dict[str, str]
"""One line of the care assistant: level (urgent | todo | info | good), text and why."""

Notice = dict[str, Any]
"""What happened when CareCircle emailed the circle: recipients, queued, sent, delivered, simulated."""
