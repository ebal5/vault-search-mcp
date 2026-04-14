"""Input validation for AI-agent-facing MCP tool arguments."""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(ValueError):
    """Raised when an agent-supplied input fails validation."""


def validate_identifier(
    name: str,
    *,
    kind: str = "identifier",
    max_len: int = 128,
) -> str:
    """Validate an identifier (field name / frontmatter key) and return it."""
    if not isinstance(name, str):
        raise ValidationError(f"{kind} must be a string, got {type(name).__name__}")
    if name == "":
        raise ValidationError(f"{kind} must not be empty")
    if len(name) > max_len:
        raise ValidationError(
            f"{kind} exceeds maximum length {max_len} (got {len(name)})"
        )
    if _CONTROL_RE.search(name):
        raise ValidationError(
            f"{kind} contains control characters; allowed: A-Z a-z 0-9 _ - ."
        )
    if not _IDENTIFIER_RE.match(name):
        raise ValidationError(
            f"{kind} contains disallowed characters "
            f"(allowed: A-Z a-z 0-9 _ - .): {name!r}"
        )
    return name


def validate_value(
    value: str,
    *,
    kind: str = "value",
    max_len: int = 1024,
) -> str:
    """Validate a free-form value string and return it."""
    if not isinstance(value, str):
        raise ValidationError(f"{kind} must be a string, got {type(value).__name__}")
    if len(value) > max_len:
        raise ValidationError(
            f"{kind} exceeds maximum length {max_len} (got {len(value)})"
        )
    if _CONTROL_RE.search(value):
        raise ValidationError(f"{kind} contains control characters")
    return value
