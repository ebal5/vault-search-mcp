"""Input validation for AI-agent-facing MCP tool arguments.

This module treats agent-supplied inputs (field names, frontmatter keys,
filter values) as adversarial. Agents produce hallucinated strings, not
typos, so validation rejects anything outside an explicit allow-list and
surfaces the ``kind`` of input in error messages so the agent can self-correct.

Design notes
------------

* Identifiers (``field`` names, frontmatter keys) use a conservative ASCII
  allow-list: ``A-Z a-z 0-9 _ - .``. The dot is kept because Obsidian users
  conventionally express nested frontmatter keys as ``nested.key``.
* Values (``metadata_filter`` right-hand side) accept any Unicode text
  except C0/C1-adjacent control characters. Empty string is valid because
  ``key == ""`` is a meaningful filter ("frontmatter key present but
  empty"). Length is capped to guard against runaway agent output.
* ``ValidationError`` inherits from :class:`ValueError` so existing
  ``except ValueError`` handlers continue to work, while callers who want
  to distinguish validation failures can catch it specifically.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence

from .exceptions import ErrorCode, VaultSearchError

__all__ = [
    "IDENTIFIER_JSON_PATTERN",
    "IDENTIFIER_MAX_LEN",
    "LIMIT_MAX",
    "ValidationError",
    "validate_identifier",
    "validate_known_key",
    "validate_pagination",
    "validate_value",
]

# Hard ceiling on paginated result size. Anchored to the FTS5 cache ceiling
# (`_MAX_RESULTS` in indexer) so that requested limit never exceeds what the
# backend actually returns — avoiding silent truncation from the agent's POV.
# Public (no `_` prefix) because schemas.py references this as the single
# source of truth for the MCP tool input schema's ``maximum`` constraint.
LIMIT_MAX = 500


# Allowed character class for identifiers (field names, frontmatter keys).
# Obsidian nested-key convention requires ``.`` as a segment separator; ``_``
# and ``-`` are common in user-authored frontmatter. Dots are only permitted
# between non-empty segments: leading, trailing, and consecutive dots would
# expand to malformed SQLite JSON paths (e.g. ``$..foo``) and surface as
# ``sqlite3.OperationalError`` instead of ``ValidationError`` (issue #14).
_IDENTIFIER_SEGMENT = r"[A-Za-z0-9_\-]+"
_IDENTIFIER_PATTERN = rf"{_IDENTIFIER_SEGMENT}(?:\.{_IDENTIFIER_SEGMENT})*"
_IDENTIFIER_RE = re.compile(rf"^{_IDENTIFIER_PATTERN}$")
_IDENTIFIER_ALLOWED_DESC = "A-Z a-z 0-9 _ -, with . as separator between non-empty segments"

# C0 controls (0x00-0x1F) + DEL (0x7F). Tabs/newlines are rejected because
# they are never meaningful inside a field name or filter value and are a
# common smuggling vector.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Default length caps. Identifiers are bounded tight because legitimate
# frontmatter keys are short; values are bounded loose because filter
# targets may be free-form human text.
_DEFAULT_IDENTIFIER_MAX_LEN = 128
_DEFAULT_VALUE_MAX_LEN = 1024

# Public constants derived from the private definitions above.
# Single source of truth for JSON Schema constraints that must match the
# runtime validation logic (used in mcp_contract.py for ``propertyNames``).
IDENTIFIER_JSON_PATTERN = _IDENTIFIER_RE.pattern
IDENTIFIER_MAX_LEN = _DEFAULT_IDENTIFIER_MAX_LEN


class ValidationError(VaultSearchError, ValueError):
    """Raised when an agent-supplied input fails validation.

    Inherits from both :class:`VaultSearchError` and :class:`ValueError` so
    code that catches ``ValueError`` keeps working. Catch ``ValidationError``
    explicitly to distinguish input-validation failures from other value errors.

    Parameters
    ----------
    message:
        Human-readable description of the validation failure.
    error_code:
        Per-instance error code; defaults to ``"VALIDATION_ERROR"``.
    hint:
        Optional short guidance for self-correction (e.g. "see schema://tools").
    did_you_mean:
        Optional list of close-match candidates (from difflib).
    allowed:
        Optional sorted list of all allowed values / keys.
    """

    error_code: ErrorCode = "VALIDATION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode = "VALIDATION_ERROR",
        hint: str | None = None,
        did_you_mean: Sequence[str] | None = None,
        allowed: Sequence[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.did_you_mean: tuple[str, ...] = tuple(did_you_mean) if did_you_mean else ()
        self.allowed: tuple[str, ...] = tuple(allowed) if allowed else ()


def validate_identifier(
    name: str,
    *,
    kind: str = "identifier",
    max_len: int = _DEFAULT_IDENTIFIER_MAX_LEN,
) -> str:
    """Validate an identifier string and return it unchanged.

    Intended for field names and frontmatter keys coming from agent input.

    Parameters
    ----------
    name:
        Candidate identifier.
    kind:
        Human-readable label for this identifier (e.g. ``"field name"``,
        ``"frontmatter key"``). Included in error messages so the agent
        can tell which input to fix.
    max_len:
        Maximum allowed length in characters. Defaults to 128.

    Returns
    -------
    str
        ``name`` verbatim when valid.

    Raises
    ------
    ValidationError
        If ``name`` is empty, exceeds ``max_len``, contains control
        characters (including NUL), attempts path traversal, has an
        empty dot-separated segment (leading/trailing/consecutive ``.``),
        or contains any character outside ``A-Z a-z 0-9 _ -`` within a
        segment.
    """
    if not isinstance(name, str):
        raise ValidationError(f"{kind} must be a string, got {type(name).__name__}")
    if name == "":
        raise ValidationError(f"{kind} must not be empty")
    if len(name) > max_len:
        raise ValidationError(f"{kind} exceeds maximum length {max_len} (got {len(name)})")
    if _CONTROL_RE.search(name):
        raise ValidationError(
            f"{kind} contains control characters; allowed: {_IDENTIFIER_ALLOWED_DESC}"
        )
    # Detect empty dot-separated segments before the generic char-class check
    # so the error message names the structural cause (issue #14). Otherwise
    # `a..b` would be reported as "disallowed characters" even though every
    # character is in the allowed set, misleading agents into stripping
    # characters instead of fixing the dot structure.
    if ".." in name or name.startswith(".") or name.endswith("."):
        raise ValidationError(
            f"{kind} has empty dot-separated segment; "
            f"use 'a.b' for nested keys (no leading/trailing/consecutive '.'): {name!r}"
        )
    if not _IDENTIFIER_RE.match(name):
        raise ValidationError(
            f"{kind} contains disallowed characters (allowed: {_IDENTIFIER_ALLOWED_DESC}): {name!r}"
        )
    return name


def validate_known_key(
    name: str,
    known_keys: Sequence[str],
    *,
    kind: str,
) -> str:
    """Validate that ``name`` appears in ``known_keys`` and return it unchanged.

    Intended for frontmatter keys (and, later, known tags / folders) where the
    index contains an authoritative allow-list and an agent may hallucinate a
    near-miss. Unknown keys raise :class:`ValidationError` with
    ``error_code="UNKNOWN_FRONTMATTER_KEY"`` and the full ``known_keys`` sorted
    list in ``allowed``; close matches (``difflib.get_close_matches``,
    ``cutoff=0.6``) appear in ``did_you_mean``.

    Parameters
    ----------
    name:
        Candidate key. Callers should pass the value through
        :func:`validate_identifier` first so SQL / path-traversal shapes are
        rejected before similarity matching.
    known_keys:
        Authoritative allow-list (from the index). Empty sequence is
        permitted; every ``name`` is unknown in that case.
    kind:
        Human-readable label (e.g. ``"frontmatter key"``) interpolated into
        the error message so the agent knows which input to fix.
    """
    if name in known_keys:
        return name
    suggestions = difflib.get_close_matches(name, known_keys, n=3, cutoff=0.6)
    if suggestions:
        msg = (
            f"Unknown {kind} {name!r}; "
            f"did you mean: {', '.join(suggestions)}? "
            f"See schema://tools for the frontmatter_keys list"
        )
    else:
        preview = ", ".join(sorted(known_keys)[:5])
        suffix = ", ..." if len(known_keys) > 5 else ""
        msg = (
            f"Unknown {kind} {name!r}; "
            f"valid keys include: {preview}{suffix}. "
            f"See schema://tools for the full list"
        )
    raise ValidationError(
        msg,
        error_code="UNKNOWN_FRONTMATTER_KEY",
        did_you_mean=suggestions,
        allowed=sorted(known_keys),
    )


def _validate_strict_int(value: object, name: str) -> None:
    """Reject non-``int`` *and* ``bool`` (which is a subclass of ``int``).

    ``isinstance(True, int)`` is ``True`` in Python, so a plain ``isinstance``
    check would silently accept ``True``/``False`` as pagination bounds.
    Splitting this out keeps the bool-trap reasoning in one place and avoids
    duplicating the error message across each paginated argument.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer, got {type(value).__name__}")


def validate_pagination(limit: int, offset: int = 0) -> None:
    """Validate paginated-query bounds, raising on out-of-range values.

    ``limit`` must be a positive integer (zero-result queries waste a round
    trip and usually indicate a hallucinated bound). ``offset`` must be
    non-negative. ``limit`` is capped at :data:`LIMIT_MAX` to keep requests
    aligned with the internal FTS5 cache ceiling.
    """
    _validate_strict_int(limit, "limit")
    _validate_strict_int(offset, "offset")
    if limit < 1:
        raise ValidationError(f"limit must be >= 1 (got {limit})")
    if limit > LIMIT_MAX:
        raise ValidationError(f"limit must be <= {LIMIT_MAX} (got {limit})")
    if offset < 0:
        raise ValidationError(f"offset must be >= 0 (got {offset})")


def validate_value(
    value: str,
    *,
    kind: str = "value",
    max_len: int = _DEFAULT_VALUE_MAX_LEN,
) -> str:
    """Validate a free-form value string and return it unchanged.

    Intended for the right-hand side of ``metadata_filter`` pairs. Unlike
    identifiers, values may contain arbitrary Unicode (including Japanese,
    spaces, punctuation) because they are compared as-is against
    frontmatter content.

    Parameters
    ----------
    value:
        Candidate value.
    kind:
        Human-readable label for this value (e.g. ``"frontmatter value"``).
        Included in error messages.
    max_len:
        Maximum allowed length in characters. Defaults to 1024.

    Returns
    -------
    str
        ``value`` verbatim when valid. Empty string is valid.

    Raises
    ------
    ValidationError
        If ``value`` exceeds ``max_len`` or contains control characters
        (0x00-0x1F or 0x7F).
    """
    if not isinstance(value, str):
        raise ValidationError(f"{kind} must be a string, got {type(value).__name__}")
    if len(value) > max_len:
        raise ValidationError(f"{kind} exceeds maximum length {max_len} (got {len(value)})")
    if _CONTROL_RE.search(value):
        raise ValidationError(f"{kind} contains control characters")
    return value
