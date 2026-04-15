"""Markdown ファイルのパース: frontmatter 抽出 + コンテンツ分離."""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# frontmatter の区切り: --- で始まり --- で終わる YAML ブロック
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# YAML パーサーが無い環境でも動くよう、軽量な自前パーサーを用意
# PyYAML があれば使い、なければ簡易パースにフォールバック
try:
    import yaml  # type: ignore[import-untyped]

    def _parse_yaml(text: str) -> dict[str, Any]:
        try:
            result = yaml.safe_load(text)
            return result if isinstance(result, dict) else {}
        except yaml.YAMLError:
            # Templater テンプレートや壊れた YAML のフォールバック
            return _parse_yaml_fallback(text)

except ImportError:

    def _parse_yaml(text: str) -> dict[str, Any]:
        """PyYAML なしの簡易パーサー。key: value の単純構造のみ対応."""
        return _parse_yaml_fallback(text)


def _parse_yaml_fallback(text: str) -> dict[str, Any]:
    """堅牢な簡易 YAML パーサー。壊れた YAML でも最大限抽出する."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # リスト項目
        if stripped.startswith("- ") and current_key is not None:
            if current_list is None:
                current_list = []
                result[current_key] = current_list
            current_list.append(stripped[2:].strip().strip("\"'"))
            continue

        # key: value
        if ":" in stripped:
            current_list = None
            k, _, v = stripped.partition(":")
            current_key = k.strip()
            v = v.strip().strip("\"'")
            if v:
                result[current_key] = v

    return result


@dataclass
class ParsedNote:
    """パース済みノートの構造.

    ``frontmatter`` は構築時に :func:`_normalize_fm` で再帰正規化される。
    ``parse_note`` 以外の経路で ``ParsedNote`` を直接構築しても SQL 層の
    str→str 不変条件が保たれる (trust boundary を dataclass に閉じ込める)。
    ``_normalize_scalar`` は str を素通しするため二重適用しても冪等。
    """

    path: str  # Vault ルートからの相対パス
    title: str
    folder: str
    content: str  # frontmatter を除いた本文
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    modified_at: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 直接構築された ParsedNote にも正規化を強制する (Reviewer D7: trust boundary).
        self.frontmatter = _normalize_fm(self.frontmatter)

    @property
    def tags_json(self) -> str:
        return json.dumps(self.tags, ensure_ascii=False)

    @property
    def frontmatter_json(self) -> str:
        # 正規化済みのはずなので default は不要。非 JSON 値が来たら TypeError を
        # 落として bug を可視化する (Reviewer A3 / D11: silent coercion を許さない).
        return json.dumps(self.frontmatter, ensure_ascii=False)


def _normalize_scalar(v: Any) -> Any:
    """frontmatter スカラー値を metadata_filter 比較向けに文字列化.

    metadata_filter は常に str 値で比較するため、YAML が返すネイティブ型
    (int / float / bool / date) を parse 時に文字列へ正規化する。これにより
    query-time の CAST が不要になり、bool が ``"1"``/``"0"`` 化する UX ワートも
    消える (Issue #15 / #49)。

    - bool → ``"true"``/``"false"`` (YAML 表記と一致)
    - int / float → ``str(v)`` (e.g. ``5`` → ``"5"``, ``4.5`` → ``"4.5"``)
    - date / datetime → ISO 8601 文字列 (``2024-01-15``)
    - None / str / その他 → そのまま

    ``isinstance(True, int)`` は Python では True なので bool 判定を先に行う。
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _normalize_fm(obj: Any) -> Any:
    """frontmatter dict を再帰的にスカラー正規化."""
    if isinstance(obj, dict):
        return {k: _normalize_fm(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_fm(v) for v in obj]
    return _normalize_scalar(obj)


def _normalize_tags(raw: Any) -> list[str]:
    """frontmatter の tags を正規化してリストで返す."""
    if isinstance(raw, list):
        return [str(t).strip().lstrip("#") for t in raw if t]
    if isinstance(raw, str):
        # カンマ区切り or スペース区切り
        return [t.strip().lstrip("#") for t in re.split(r"[,\s]+", raw) if t.strip()]
    return []


def _extract_inline_tags(content: str) -> list[str]:
    """本文中の #tag を抽出（ただし見出しの # は除外）."""
    return re.findall(r"(?:^|(?<=\s))#([a-zA-Z\u3000-\u9fff\uff00-\uffef][\w/\-]*)", content)


def _extract_title(content: str, path: str) -> str:
    """タイトルを推定: 最初の H1 → ファイル名."""
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return Path(path).stem


def parse_note(file_path: Path, vault_root: Path) -> ParsedNote | None:
    """Markdown ファイルをパースして ParsedNote を返す.

    バイナリファイルやパース不能の場合は None.
    """
    rel_path = str(file_path.relative_to(vault_root)).replace("\\", "/")
    folder = str(file_path.parent.relative_to(vault_root)).replace("\\", "/")
    if folder == ".":
        folder = ""

    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None

    # frontmatter 分離 — 正規化は ``ParsedNote.__post_init__`` に一任 (Issue #15 / #49).
    fm: dict[str, Any] = {}
    content = text
    m = _FM_RE.match(text)
    if m:
        fm = _parse_yaml(m.group(1))
        content = text[m.end() :]

    # タグ: frontmatter + インライン
    tags = _normalize_tags(fm.get("tags", []))
    inline_tags = _extract_inline_tags(content)
    all_tags = list(dict.fromkeys(tags + inline_tags))  # 順序保持 dedup

    # エイリアス
    aliases_raw = fm.get("aliases", [])
    aliases = aliases_raw if isinstance(aliases_raw, list) else [str(aliases_raw)]

    # 日付
    created = str(fm.get("created_at", fm.get("date", fm.get("created", ""))))
    modified = str(fm.get("modified_at", fm.get("updated_at", fm.get("updated", ""))))

    title = str(fm.get("title", "")) or _extract_title(content, rel_path)

    return ParsedNote(
        path=rel_path,
        title=title,
        folder=folder,
        content=content.strip(),
        tags=all_tags,
        created_at=created,
        modified_at=modified,
        frontmatter=fm,
        aliases=aliases,
    )
