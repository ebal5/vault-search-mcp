"""Input validation for AI-agent-facing MCP tool arguments.

This module treats agent-supplied inputs (field names, frontmatter keys,
filter values) as adversarial. Agents produce hallucinated strings, not
typos, so validation rejects anything outside an explicit allow-list and
surfaces the ``kind`` of input in error messages so the agent can self-correct.

Design notes
------------

* Identifiers (``field`` names, frontmatter keys) accept Unicode word
  characters (``\\w``), hyphens, and dots as segment separators. This allows
  non-ASCII frontmatter keys such as ``タイトル`` that Obsidian users author
  natively (#33). Control characters, path separators, and other punctuation
  are still rejected. The dot is kept for nested-key notation (``nested.key``).
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
from collections.abc import Iterable, Mapping, Sequence

from .exceptions import ErrorCode, VaultSearchError

__all__ = [
    "IDENTIFIER_JSON_PATTERN",
    "IDENTIFIER_MAX_LEN",
    "LIMIT_MAX",
    "ValidationError",
    "format_unknown_keys_message",
    "normalize_folder",
    "validate_identifier",
    "validate_known_keys",
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
_IDENTIFIER_SEGMENT = r"[\w\-]+"
_IDENTIFIER_PATTERN = rf"{_IDENTIFIER_SEGMENT}(?:\.{_IDENTIFIER_SEGMENT})*"
_IDENTIFIER_RE = re.compile(rf"^{_IDENTIFIER_PATTERN}$")
_IDENTIFIER_ALLOWED_DESC = "Unicode word chars (\\w) and hyphen, with . as segment separator"

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
        Optional list of close-match candidates (from difflib). Populated for
        single-unknown-key errors for backward compatibility; multi-key errors
        expose per-key candidates via ``unknown_keys`` instead.
    allowed:
        Optional sorted list of all allowed values / keys.
    unknown_keys:
        Optional per-key close-match map for batched unknown-key reports
        (``error_code="UNKNOWN_FRONTMATTER_KEY"`` from
        :func:`~vault_search.filter.parse_metadata_filter`). Each entry maps an
        unknown key to its suggestion tuple (empty tuple when no close match
        exists). Single-key errors still populate this with one entry so agents
        can inspect the structured form uniformly (#123).
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
        unknown_keys: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.did_you_mean: tuple[str, ...] = tuple(did_you_mean) if did_you_mean else ()
        self.allowed: tuple[str, ...] = tuple(allowed) if allowed else ()
        self.unknown_keys: dict[str, tuple[str, ...]] = (
            {k: tuple(v) for k, v in unknown_keys.items()} if unknown_keys else {}
        )


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
        or contains a character outside Unicode word characters (``\\w``) and
        hyphen within a segment (e.g. spaces, punctuation, path separators).
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


def format_unknown_keys_message(
    unknowns: Mapping[str, Sequence[str]],
    kind: str,
    known_keys: Sequence[str],
    *,
    schema_ref: str = "schema://tools",
    registry_label: str = "frontmatter_keys list",
) -> str:
    """Build a unified unknown-key error message covering 1 and N unknown keys.

    MCP / frontmatter-specific wording is injected via ``schema_ref`` and
    ``registry_label`` instead of hardcoded strings (#138).

    ``known_keys`` は順不同で渡してよい — 関数内で ``sorted()`` してから preview
    を組み立てる。callsite に「ソート済みで渡す」暗黙契約を強いず、drift
    リスクを低減する (Round 1 review D4)。
    """
    allowed_sorted = sorted(known_keys)

    if len(unknowns) == 1:
        ((name, suggestions),) = unknowns.items()
        if suggestions:
            return (
                f"Unknown {kind} {name!r}; "
                f"did you mean: {', '.join(suggestions)}? "
                f"See {schema_ref} for the {registry_label}"
            )
        preview = ", ".join(allowed_sorted[:5])
        suffix = ", ..." if len(allowed_sorted) > 5 else ""
        # Single-key no-match keeps the legacy "full list" wording (kind-agnostic)
        # for bit-identicalness with pre-#138 messages. registry_label is reserved
        # for did-you-mean / multi-key branches where agents need a navigation
        # target inside schema://tools.
        return (
            f"Unknown {kind} {name!r}; "
            f"valid keys include: {preview}{suffix}. "
            f"See {schema_ref} for the full list"
        )

    # Multi-key path. Sort by unknown key name so the message is deterministic
    # under input reordering (R2-3) and matches ``err.unknown_keys`` iteration
    # order (R3 D-1).
    sorted_unknowns = dict(sorted(unknowns.items()))
    parts = [
        f"{k!r} (did you mean: {', '.join(s)})" if s else f"{k!r} (no close match)"
        for k, s in sorted_unknowns.items()
    ]
    keys_str = ", ".join(parts)
    if any(not s for s in sorted_unknowns.values()):
        preview = ", ".join(allowed_sorted[:5])
        suffix = ", ..." if len(allowed_sorted) > 5 else ""
        return (
            f"Unknown {kind}s: {keys_str}. "
            f"Valid keys include: {preview}{suffix}. "
            f"See {schema_ref} for the {registry_label}"
        )
    return f"Unknown {kind}s: {keys_str}. See {schema_ref} for the {registry_label}"


def validate_known_keys(
    names: Iterable[str],
    known_keys: Sequence[str],
    *,
    kind: str,
    schema_ref: str = "schema://tools",
    registry_label: str = "frontmatter_keys list",
) -> None:
    """Validate that every name in ``names`` appears in ``known_keys`` (batch).

    Used by filter.py's ``parse_metadata_filter`` to report all unknown keys
    in one :class:`ValidationError` (``error_code="UNKNOWN_FRONTMATTER_KEY"``)
    so agents can self-correct in a single round-trip (#123/#141).

    Parameters
    ----------
    names:
        Candidate names, iterated once. Callers should pass each name through
        :func:`validate_identifier` first.
    known_keys:
        Authoritative allow-list (from the index). Empty is permitted; every
        name is then unknown.
    kind:
        Human-readable singular label (e.g. ``"frontmatter key"``) used in the
        error message. Multi-key messages append ``"s"`` to pluralize.
    schema_ref, registry_label:
        Inject the resource reference and registry label so validation stays
        kind-agnostic (#138). Defaults match the frontmatter_keys use case.
    """
    unknowns: dict[str, tuple[str, ...]] = {}
    for name in names:
        if name in known_keys:
            continue
        if name in unknowns:  # dedupe repeated hits in the iterable
            continue
        unknowns[name] = tuple(difflib.get_close_matches(name, known_keys, n=3, cutoff=0.6))

    if not unknowns:
        return

    sorted_unknowns = dict(sorted(unknowns.items()))
    all_candidates = tuple(dict.fromkeys(c for s in sorted_unknowns.values() for c in s))
    raise ValidationError(
        format_unknown_keys_message(
            sorted_unknowns,
            kind,
            known_keys,
            schema_ref=schema_ref,
            registry_label=registry_label,
        ),
        error_code="UNKNOWN_FRONTMATTER_KEY",
        did_you_mean=all_candidates,
        allowed=sorted(known_keys),
        unknown_keys=sorted_unknowns,
    )


def normalize_folder(folder: str) -> str | None:
    """入力 folder を canonical 形式に正規化する。

    - ``\\`` → ``/``
    - 連続 ``/`` を単一化
    - 先頭 ``/`` を strip
    - 末尾 ``/`` を strip
    - 結果が空なら ``None`` を返す (フィルタなし — ``'/'`` や ``''`` も吸収)

    Returns
    -------
    str | None
        正規化済みフォルダパス。空になった場合は ``None``。

    Notes
    -----
    path traversal (``..`` 等) の検出はこの関数の scope 外。
    """
    normalized = re.sub(r"/+", "/", folder.replace("\\", "/")).strip("/")
    return normalized if normalized else None


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
