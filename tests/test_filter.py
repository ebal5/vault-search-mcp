"""build_sql_fragment の SQL コントラクト (Issue #15 / #49 後).

parser.py が frontmatter スカラーを parse 時に str 正規化する前提のため、
``build_sql_fragment`` が生成する SQL は単純な str→str 比較のみを扱う。
本テストはその不変条件を in-memory SQLite で固定化する:

- eq / ne / in が str スカラー / str 配列に対して対称に動作
- キー欠落ノートは eq でマッチせず、ne でもマッチしない (3 値論理)
- 空配列・単一要素・重複要素の in 境界

int / bool / float / date の正規化は ``tests/test_parser.py`` と
``tests/test_indexer.py`` の end-to-end テストで検証する。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from vault_search.filter import MetadataCondition, build_sql_fragment, parse_metadata_filter
from vault_search.validation import ValidationError


@pytest.fixture
def conn() -> sqlite3.Connection:
    """parser 正規化後のストレージを模した str-only フィクスチャ."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE notes (path TEXT PRIMARY KEY, frontmatter TEXT)")
    rows: list[tuple[str, dict[str, object]]] = [
        ("hi.md", {"priority": "5"}),
        ("mid.md", {"priority": "3"}),
        ("flag-on.md", {"archived": "true"}),
        ("flag-off.md", {"archived": "false"}),
        ("no-key.md", {"other": "x"}),
        ("arr.md", {"levels": ["1", "2", "3"]}),
        ("arr-single.md", {"levels": ["1"]}),
        # Normalized bool array: YAML ``flags: [true, false]`` → ``["true", "false"]``.
        # ne / in の配列要素経路で bool 正規化後の str マッチが動くことを pin する。
        ("bool-arr.md", {"flags": ["true", "false"]}),
        ("bool-arr-true.md", {"flags": ["true"]}),
    ]
    for path, fm in rows:
        c.execute("INSERT INTO notes VALUES (?, ?)", (path, json.dumps(fm)))
    c.commit()
    return c


def _select(conn: sqlite3.Connection, cond: MetadataCondition) -> set[str]:
    fragment, params = build_sql_fragment(cond)
    sql = f"SELECT path FROM notes n WHERE 1=1 {fragment}"
    return {row[0] for row in conn.execute(sql, params).fetchall()}


# ---------------------------------------------------------------------------
# eq
# ---------------------------------------------------------------------------


def test_eq_scalar_str_match(conn: sqlite3.Connection) -> None:
    """str→str eq でマッチ."""
    hits = _select(conn, MetadataCondition("priority", "eq", "5"))
    assert hits == {"hi.md"}


def test_eq_array_element_match(conn: sqlite3.Connection) -> None:
    """配列フィールドは要素含有判定."""
    hits = _select(conn, MetadataCondition("levels", "eq", "2"))
    assert hits == {"arr.md"}


def test_eq_missing_key_excluded(conn: sqlite3.Connection) -> None:
    """キー欠落ノートは eq でマッチしない."""
    hits = _select(conn, MetadataCondition("priority", "eq", "5"))
    assert "no-key.md" not in hits


# ---------------------------------------------------------------------------
# ne
# ---------------------------------------------------------------------------


def test_ne_scalar_excludes_matching_value(conn: sqlite3.Connection) -> None:
    """ne は一致ノートを除外、他の値は残す."""
    hits = _select(conn, MetadataCondition("priority", "ne", "5"))
    assert hits == {"mid.md"}


def test_ne_bool_string_symmetric(conn: sqlite3.Connection) -> None:
    """bool 正規化後の "true"/"false" に対しても対称."""
    hits = _select(conn, MetadataCondition("archived", "ne", "true"))
    assert hits == {"flag-off.md"}


def test_ne_missing_key_excluded(conn: sqlite3.Connection) -> None:
    """キー欠落ノートは ne でもマッチしない (eq/ne 両方から除外、3 値論理)."""
    hits = _select(conn, MetadataCondition("priority", "ne", "5"))
    assert "no-key.md" not in hits


def test_ne_array_excludes_element_containing(conn: sqlite3.Connection) -> None:
    """配列内に value が含まれる場合 ne はマッチしない."""
    hits = _select(conn, MetadataCondition("levels", "ne", "2"))
    # arr.md は 2 を含む → 除外。arr-single.md は 1 のみ → マッチ
    assert hits == {"arr-single.md"}


# ---------------------------------------------------------------------------
# ne 配列型境界値テスト (Issue #43)
#
# 仕様まとめ:
#   - 空配列 []         : key が存在し要素が一切ない → ne にマッチ
#   - 1 要素一致        : 配列内に value が見つかる → 除外
#   - 多要素 (一致含む) : 1 つでも value を含めば → 除外
#   - 重複要素          : NOT EXISTS は重複の有無を問わず同じ → 除外
#   - キー missing      : IS NOT NULL ガードで除外 (eq と対称の 3 値論理)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fm, ne_value, should_match",
    [
        ({"tags": []}, "x", True),
        ({"tags": ["x"]}, "x", False),
        ({"tags": ["x", "y", "z"]}, "x", False),
        ({"tags": ["x", "x"]}, "x", False),
        ({"other": "y"}, "x", False),
    ],
    ids=[
        "empty-array",
        "single-element-match",
        "multi-element-has-match",
        "duplicate-elements",
        "missing-key",
    ],
)
def test_ne_array_boundary_values(
    conn: sqlite3.Connection,
    fm: dict[str, object],
    ne_value: str,
    should_match: bool,
) -> None:
    """ne 演算子の配列型境界値テスト (Issue #43).

    仕様:
    - 空配列はマッチ: key が存在し value を含まない → NOT EXISTS = True
    - 配列内に value が存在すれば除外 (1 要素 / 多要素 / 重複どれも同様)
    - キー missing は eq/ne 両方からマッチしない (3 値論理)
    """
    conn.execute("INSERT INTO notes VALUES (?, ?)", ("target.md", json.dumps(fm)))
    conn.commit()
    hits = _select(conn, MetadataCondition("tags", "ne", ne_value))
    if should_match:
        assert "target.md" in hits, f"target.md (fm={fm!r}) should match ne={ne_value!r}"
    else:
        assert "target.md" not in hits, f"target.md (fm={fm!r}) should not match ne={ne_value!r}"


# ---------------------------------------------------------------------------
# in
# ---------------------------------------------------------------------------


def test_in_scalar_any_match(conn: sqlite3.Connection) -> None:
    hits = _select(conn, MetadataCondition("priority", "in", ("5", "3")))
    assert hits == {"hi.md", "mid.md"}


def test_in_single_element(conn: sqlite3.Connection) -> None:
    """1 要素 tuple (placeholders 生成の fence-post ガード)."""
    hits = _select(conn, MetadataCondition("priority", "in", ("5",)))
    assert hits == {"hi.md"}


def test_in_array_any_element_match(conn: sqlite3.Connection) -> None:
    hits = _select(conn, MetadataCondition("levels", "in", ("2", "99")))
    assert hits == {"arr.md"}


# ---------------------------------------------------------------------------
# Normalized bool array element matching (Round 3 Reviewer C finding 5)
#
# YAML ``flags: [true, false]`` は parser で ``["true", "false"]`` へ
# 正規化される。ne / in 演算子の array-element path で bool 正規化文字列が
# 期待通り扱われることを固定化する。
# ---------------------------------------------------------------------------


def test_ne_bool_array_excludes_element_containing(conn: sqlite3.Connection) -> None:
    """配列内に "true" を含むノートは ne "true" でマッチしない."""
    hits = _select(conn, MetadataCondition("flags", "ne", "true"))
    # bool-arr.md, bool-arr-true.md はどちらも "true" を含む → 除外
    assert hits == set()


def test_in_bool_array_element_match(conn: sqlite3.Connection) -> None:
    """in 演算子が bool 正規化後の "false" を含む配列をヒットさせる."""
    hits = _select(conn, MetadataCondition("flags", "in", ("false",)))
    assert hits == {"bool-arr.md"}


# ---------------------------------------------------------------------------
# eq / ne partition property (Reviewer C2)
# ---------------------------------------------------------------------------


def test_eq_ne_exclude_null_value(conn: sqlite3.Connection) -> None:
    """YAML null で格納された値は eq / ne どちらにも含まれない (3 値論理).

    ``archived: null`` のノートを追加し、どちらの演算でもマッチしないことを
    ``IS NOT NULL`` ガードの回帰ガードとして固定化する。
    """
    conn.execute(
        "INSERT INTO notes VALUES (?, ?)",
        ("null-val.md", json.dumps({"archived": None})),
    )
    eq_hits = _select(conn, MetadataCondition("archived", "eq", "true"))
    ne_hits = _select(conn, MetadataCondition("archived", "ne", "true"))
    assert "null-val.md" not in eq_hits
    assert "null-val.md" not in ne_hits


def test_eq_ne_partition_on_present_key(conn: sqlite3.Connection) -> None:
    """キーが存在するノートに対し eq_hits と ne_hits は disjoint かつ和集合で
    キー保持ノート全体をカバーする。"""
    keyed_paths = {
        row[0]
        for row in conn.execute(
            "SELECT path FROM notes WHERE json_extract(frontmatter, '$.priority') IS NOT NULL"
        ).fetchall()
    }
    for value in ("5", "3", "99"):
        eq_hits = _select(conn, MetadataCondition("priority", "eq", value))
        ne_hits = _select(conn, MetadataCondition("priority", "ne", value))
        assert eq_hits.isdisjoint(ne_hits), f"eq and ne overlap for value={value!r}"
        assert eq_hits | ne_hits == keyed_paths, (
            f"eq ∪ ne != keyed_paths for value={value!r}: "
            f"eq={eq_hits} ne={ne_hits} keyed={keyed_paths}"
        )


def test_eq_ne_partition_on_array_key(conn: sqlite3.Connection) -> None:
    """配列キー (levels) に対しても eq/ne は disjoint かつ和集合でキー保持ノート全体をカバーする。

    配列 ne は ``NOT EXISTS(json_each ...)`` という非対称 SQL を使う。
    scalar ne と同じ partition property が成立することを固定化し、
    配列分岐の regression を防ぐ。
    """
    # arr.md: {"levels": ["1","2","3"]},  arr-single.md: {"levels": ["1"]}
    keyed_array = {"arr.md", "arr-single.md"}
    for value in ("1", "2", "99"):
        eq_hits = _select(conn, MetadataCondition("levels", "eq", value))
        ne_hits = _select(conn, MetadataCondition("levels", "ne", value))
        assert eq_hits.isdisjoint(ne_hits), f"eq and ne overlap for value={value!r}"
        assert eq_hits | ne_hits == keyed_array, (
            f"eq ∪ ne != keyed_array for value={value!r}: "
            f"eq={eq_hits} ne={ne_hits} keyed={keyed_array}"
        )


# ---------------------------------------------------------------------------
# Error message guidance (Round 3 Reviewer B finding)
#
# frontmatter スカラーは index 時に str 正規化されるため、agent が
# ``{"priority": 5}`` のように非 str 値を渡すと ValidationError になる。
# メッセージには単に「string が必要」と出すだけでなく、「stringify せよ」という
# 修正方針を含める (agent が "got int" だけだと auto-coerce を期待して
# retry するため)。
# ---------------------------------------------------------------------------


def test_implicit_eq_non_string_error_hints_stringification() -> None:
    """暗黙 eq に int を渡したエラーが stringify の指示を含む."""
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter({"priority": 5})
    msg = str(exc.value)
    assert "priority" in msg
    assert "int" in msg
    # 正規化の事実と修正方針を明示
    assert "normalized to strings" in msg
    assert '"5"' in msg  # 具体例


def test_in_operator_non_string_item_error_hints_stringification() -> None:
    """in のリスト要素に bool を渡したエラーも stringify を指示する."""
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter({"archived": {"in": [True]}})
    msg = str(exc.value)
    assert "bool" in msg
    assert "normalized to strings" in msg
    assert '"true"' in msg


def test_ne_operator_non_string_value_error_hints_stringification() -> None:
    """ne に float を渡したエラーも stringify を指示する."""
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter({"score": {"ne": 4.5}})
    msg = str(exc.value)
    assert "float" in msg
    assert "normalized to strings" in msg
    assert '"4.5"' in msg


# ---------------------------------------------------------------------------
# Negative tests: LLM-hallucinated operator forms (Issue #41)
#
# LLM が MongoDB 風演算子 ($in / $eq 等) や非サポート演算子を生成したとき、
# および in の引数に string を渡したときに ValidationError を送出することを
# spec として固定化する。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filter_input",
    [
        {"tags": {"$in": ["a", "b"]}},  # MongoDB 風 $ prefix
        {"tags": {"$eq": "x"}},
        {"tags": {"$ne": "x"}},
        {"tags": {"eq": "x"}},  # dict 形式の eq (bare string が正しい)
        {"tags": {"==": "x"}},  # 比較演算子記号
        {"tags": {"lt": 1}},  # 未サポート比較演算子
        {"tags": {"in": "a_string"}},  # in にリストでなく string を渡す
    ],
)
def test_unsupported_operator_raises_validation_error(filter_input: dict) -> None:
    """LLM がハルシネーションしがちな入力は ValidationError を送出する (Issue #41)."""
    with pytest.raises(ValidationError):
        parse_metadata_filter(filter_input)


@pytest.mark.parametrize(
    "filter_input",
    [
        {"tags": {"$in": ["a", "b"]}},
        {"tags": {"$eq": "x"}},
        {"tags": {"$ne": "x"}},
        {"tags": {"eq": "x"}},
        {"tags": {"==": "x"}},
        # Note: range aliases (lt/gt/after/between/…) are excluded here because
        # they route to UNSUPPORTED_RANGE_OPERATOR with a different message.
        # They are fully covered by TestRangeOperatorAliasDetection.
    ],
)
def test_invalid_operator_hint_mentions_supported_forms(filter_input: dict) -> None:
    """未サポート演算子エラーのヒントに対応形式 (in / ne / bare string) が含まれる.

    エージェントがエラーメッセージを読んで自己修正できるよう、
    サポートされている構文 (ne / in / bare string) がヒントに明示されること。
    range alias は TestRangeOperatorAliasDetection で別途検証する。
    """
    with pytest.raises(ValidationError) as exc:
        parse_metadata_filter(filter_input)
    msg = str(exc.value)
    assert "in" in msg, f"'in' not found in error message: {msg}"
    assert "ne" in msg, f"'ne' not found in error message: {msg}"
    assert "string" in msg, f"'string' not found in error message: {msg}"


# ---------------------------------------------------------------------------
# Operator Literal type consolidation (Issue #13 b)
# ---------------------------------------------------------------------------


def test_operator_literal_exported_from_filter() -> None:
    """filter.py が Operator 型を公開し、eq/ne/in の 3 演算子を含むこと.

    単一 source of truth として Operator Literal を filter.py で定義し、
    他箇所が重複定義なしに参照できることを確認する。
    """
    import typing

    import vault_search.filter as filter_mod

    assert hasattr(filter_mod, "Operator"), (
        "filter.py must expose Operator Literal type as single source of truth for operators"
    )
    args = set(typing.get_args(filter_mod.Operator))
    assert args == {"eq", "ne", "in"}, f"Operator must cover exactly eq/ne/in: {args!r}"


def test_explicit_ops_derived_from_operator() -> None:
    """_EXPLICIT_OPS が Operator から eq を除いた派生であること."""
    import typing

    import vault_search.filter as filter_mod

    assert hasattr(filter_mod, "Operator"), "Operator must be defined to derive _EXPLICIT_OPS"
    assert hasattr(filter_mod, "_EXPLICIT_OPS"), "_EXPLICIT_OPS must be defined in filter.py"

    expected = frozenset(op for op in typing.get_args(filter_mod.Operator) if op != "eq")
    actual = frozenset(filter_mod._EXPLICIT_OPS)

    assert actual == expected, (
        f"_EXPLICIT_OPS {filter_mod._EXPLICIT_OPS!r} must match Operator-minus-eq {expected!r}"
    )
    assert "eq" not in filter_mod._EXPLICIT_OPS, "'eq' must not appear in _EXPLICIT_OPS"


# ---------------------------------------------------------------------------
# Unknown frontmatter key detection + did_you_mean (Issue #19)
#
# parse_metadata_filter に known_keys を渡すと、入力キーが known_keys 外の場合に
# ValidationError (error_code="UNKNOWN_FRONTMATTER_KEY") を送出する。
# difflib.get_close_matches で候補を提示し、hint に schema://tools を含める。
# ---------------------------------------------------------------------------


class TestParseMetadataFilterUnknownKey:
    """parse_metadata_filter の known_keys による未知キー検出を検証する (Issue #19).

    Red フェーズ: 以下の未実装の振る舞いが FAIL することを確認する。
    - known_keys 外のキーは ValidationError (error_code=UNKNOWN_FRONTMATTER_KEY)
    - did_you_mean に difflib が提示した候補が含まれる
    - hint に "schema://tools" が含まれる
    - known_keys=None は後方互換 (従来通り通る)
    - known_keys に含まれるキーは通る
    - known_keys=[] は全キーが unknown (ValidationError)
    - 近似候補なしでも allowed に known_keys が含まれる
    """

    def test_typo_key_raises_with_did_you_mean(self) -> None:
        """タイポキーで ValidationError を送出し、did_you_mean に修正候補が含まれる."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"priorty": "5"}, known_keys=["priority", "status"])
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        assert "priority" in err.did_you_mean
        assert "schema://tools" in str(err)  # message に含まれる (hint は削除済み)
        assert err.hint is None

    def test_known_key_passes(self) -> None:
        """known_keys に含まれるキーは ValidationError を送出しない."""
        result = parse_metadata_filter({"priority": "5"}, known_keys=["priority", "status"])
        assert len(result) == 1
        assert result[0].key == "priority"

    def test_known_keys_none_backward_compat(self) -> None:
        """known_keys=None (デフォルト) は後方互換 — 従来通り通る."""
        result = parse_metadata_filter({"xyz": "v"}, known_keys=None)
        assert len(result) == 1
        assert result[0].key == "xyz"

    def test_empty_known_keys_rejects_any_key(self) -> None:
        """known_keys=[] のとき全キーが unknown として ValidationError を送出する."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"xyz": "v"}, known_keys=[])
        assert exc.value.error_code == "UNKNOWN_FRONTMATTER_KEY"

    def test_no_close_match_still_raises_with_allowed(self) -> None:
        """近似候補なし (cutoff=0.6 未満) でもエラーを送出し、allowed に known_keys を含める."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"nonexistent": "v"},
                known_keys=["status", "priority", "tags"],
            )
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        # did_you_mean は空 sequence (近似なし)
        assert len(err.did_you_mean) == 0
        # allowed には known_keys のソート済みリストが含まれる
        assert set(err.allowed) == {"status", "priority", "tags"}

    def test_unknown_key_message_includes_suggestions(self) -> None:
        """UNKNOWN_FRONTMATTER_KEY のメッセージに did_you_mean 候補が含まれる (F1 fix)."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"priorty": "5"}, known_keys=["priority", "status"])
        msg = str(exc.value)
        assert "priority" in msg, (
            "did_you_mean candidate 'priority' must appear in error message "
            f"for agent DX; got: {msg!r}"
        )

    def test_no_close_match_message_includes_allowed_keys(self) -> None:
        """近似候補なし時もメッセージに valid keys が含まれる (F2 fix)."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"nonexistent": "v"},
                known_keys=["status", "priority", "tags"],
            )
        msg = str(exc.value)
        # At least one known key should appear in the message for agent self-correction
        assert any(k in msg for k in ["status", "priority", "tags"]), (
            "At least one known key must appear in error message when no close "
            f"match exists; got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Multiple unknown key collection (Issue #123)
#
# 複数 unknown key が 1 回の ValidationError にまとめて報告されることを pin する。
# 旧挙動は「最初の 1 件で raise」で、agent が typo を 2 つ同時に hallucinate
# した場合 2 round-trip が必要だった。LLM agent は複数キーを同時に間違えやすい
# ため、全 unknown key を collect してから一括報告することで self-correction
# 効率を改善する。
# ---------------------------------------------------------------------------


class TestParseMetadataFilterMultipleUnknownKeys:
    """parse_metadata_filter が複数 unknown key を batch 収集する (Issue #123).

    ValidationError に新規属性 ``unknown_keys: dict[str, tuple[str, ...]]`` を
    追加し、各 unknown key に対する did_you_mean 候補を構造化保持する。
    単一 key 時は従来の ``did_you_mean`` / ``allowed`` 属性も populated のまま
    (backward compat)。
    """

    def test_multiple_unknown_keys_reported_together(self) -> None:
        """2 つ以上の unknown key が 1 回の ValidationError で報告される.

        ``known_keys`` にも ``"priority"`` を含めたケースで「メッセージに
        ``"priority"`` が含まれる」だけだと valid-keys preview 経由で通過する
        vacuous pass 余地がある。構造化属性 ``unknown_keys`` と
        ``did you mean: priority`` の逐語 pattern の両方で pin する。
        """
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"priorty": "5", "statu": "active"},
                known_keys=["priority", "status"],
            )
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        # 構造化属性で両キー batch 収集を primary に検証
        assert set(err.unknown_keys.keys()) == {"priorty", "statu"}
        msg = str(err)
        # 両方の unknown key 名がメッセージに含まれる (first-fail-only なら片方欠落)
        assert "priorty" in msg, f"first unknown key missing from message: {msg!r}"
        assert "statu" in msg, f"second unknown key missing from message: {msg!r}"
        # 候補が "did you mean: ..." 逐語 pattern で現れることを pin
        # (単に ``"priority" in msg`` だと valid-keys preview 経由で vacuous に通る)
        assert "did you mean: priority" in msg, (
            f"suggestion for 'priorty' must appear in 'did you mean:' pattern; got {msg!r}"
        )
        assert "did you mean: status" in msg, (
            f"suggestion for 'statu' must appear in 'did you mean:' pattern; got {msg!r}"
        )

    def test_unknown_keys_attribute_structured_per_key(self) -> None:
        """unknown_keys 属性が各 key ごとの did_you_mean 候補を dict で保持する."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"priorty": "5", "statu": "active"},
                known_keys=["priority", "status", "tags"],
            )
        err = exc.value
        assert hasattr(err, "unknown_keys"), (
            "ValidationError must expose unknown_keys dict for multi-key DX"
        )
        # dict[str, tuple[str, ...]] — キーは unknown key、値は候補 tuple
        assert set(err.unknown_keys.keys()) == {"priorty", "statu"}
        assert "priority" in err.unknown_keys["priorty"]
        assert "status" in err.unknown_keys["statu"]

    def test_three_unknown_keys_all_collected(self) -> None:
        """3 つの unknown key すべてが報告され、first-fail-only が再発しないことを pin."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"foo": "a", "bar": "b", "baz": "c"},
                known_keys=["priority"],
            )
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        assert set(err.unknown_keys.keys()) == {"foo", "bar", "baz"}, (
            "all 3 unknown keys must be collected (regression guard against "
            f"first-fail-only); got {err.unknown_keys!r}"
        )

    def test_mixed_known_and_unknown_only_reports_unknown(self) -> None:
        """known key と unknown key が混在する場合、unknown のみ報告される."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"priority": "5", "typo1": "a", "typo2": "b"},
                known_keys=["priority", "status"],
            )
        err = exc.value
        assert set(err.unknown_keys.keys()) == {"typo1", "typo2"}
        assert "priority" not in err.unknown_keys, (
            f"known keys must not appear in unknown_keys; got {err.unknown_keys!r}"
        )

    def test_single_unknown_key_backward_compat(self) -> None:
        """unknown key が 1 つの場合、従来の did_you_mean 属性も populated."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"priorty": "5"}, known_keys=["priority", "status"])
        err = exc.value
        # 新属性にも 1 エントリある
        assert set(err.unknown_keys.keys()) == {"priorty"}
        assert "priority" in err.unknown_keys["priorty"]
        # 従来の did_you_mean / allowed 属性は単一 key 時の backward compat で維持
        assert "priority" in err.did_you_mean
        assert set(err.allowed) == {"priority", "status"}

    def test_multiple_unknown_keys_allowed_full_sorted(self) -> None:
        """multi-unknown 時も allowed は known_keys をソート済み tuple で含む."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"typo1": "a", "typo2": "b"},
                known_keys=["tags", "priority", "status"],
            )
        err = exc.value
        # 集合比較ではなく順序付き tuple で pin (C2 review: "sorted" 主張の担保)
        assert tuple(err.allowed) == ("priority", "status", "tags"), (
            f"allowed must be sorted ascending; got {err.allowed!r}"
        )

    def test_multi_key_no_candidates_message_includes_valid_keys(self) -> None:
        """全キーに候補なしの multi-key batch で message が valid keys を列挙 (C3).

        ``known_keys=["zzz"]`` と close match 不能な key の組み合わせで、
        ``_raise_unknown_keys`` の「候補なし混在」分岐が正しく valid keys
        preview を付加することを pin する。
        """
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"foo": "a", "bar": "b"},
                known_keys=["zzz"],
            )
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY"
        # 各 key の候補は空 tuple
        assert err.unknown_keys == {"foo": (), "bar": ()}, (
            f"all keys must have empty suggestions; got {err.unknown_keys!r}"
        )
        msg = str(err)
        # message に valid keys preview と "no close match" が含まれる
        assert "zzz" in msg, f"valid keys preview missing; got {msg!r}"
        assert "no close match" in msg, f"no-close-match annotation missing; got {msg!r}"

    def test_known_keys_none_allows_multiple_keys(self) -> None:
        """known_keys=None で複数キーを渡しても batch 収集経路に落ちず通る (C5).

        ``if known_keys is not None:`` の早期 skip が壊れた場合の regression
        guard。multi-key 収集経路が誤って None でも発動する変更を検知する。
        """
        conditions = parse_metadata_filter(
            {"foo": "a", "bar": "b", "baz": "c"},
            known_keys=None,
        )
        assert len(conditions) == 3
        assert {c.key for c in conditions} == {"foo", "bar", "baz"}

    def test_unknown_key_takes_precedence_over_bad_operator(self) -> None:
        """3-pass 化で unknown key 検出が op/value error より先に発火する (A3).

        旧 per-key interleave では ``{"known": {"bad_op": 5}, "typo": "x"}`` が
        bad_op で先に fail していたが、3-pass 化後は pass 2 が typo を検出して
        UNKNOWN_FRONTMATTER_KEY を raise する。この契約変更を明示的に pin する。
        """
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"priority": {"bad_op": 5}, "typo": "x"},
                known_keys=["priority", "status"],
            )
        err = exc.value
        assert err.error_code == "UNKNOWN_FRONTMATTER_KEY", (
            "unknown key detection must precede per-entry operator validation; "
            f"got error_code={err.error_code!r}"
        )
        assert "typo" in err.unknown_keys


# ---------------------------------------------------------------------------
# Round 1 review findings — multi-key UX refinements (Issue #123 follow-ups)
#
# #123 PR #135 round 1 レビューで指摘された以下を固定化:
# - A2/B2: multi-key 時も did_you_mean に全候補 flatten (agent が旧 API で分岐
#   しても候補ゼロにならない)
# - B3: 混在 suggestion 時に「候補なし」が明示される ("no close match")
# - B5: schema reference 文言を single/multi で統一 ("frontmatter_keys list")
# ---------------------------------------------------------------------------


class TestMultiKeyMessageRefinements:
    """Round 1 review の multi-key message / 属性対称性を pin する."""

    def test_multi_key_did_you_mean_flattened_for_backward_compat(self) -> None:
        """multi-key 時も did_you_mean に全候補が flatten で入る (A2/B2).

        旧 agent コードが ``if err.did_you_mean:`` で候補提示を分岐している場合、
        multi-key 時に空 tuple になると「候補ゼロ」扱いで分岐が崩れる。
        単一/複数で非対称にならないよう全候補を flatten populate する。
        """
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"priorty": "5", "statu": "active"},
                known_keys=["priority", "status"],
            )
        err = exc.value
        # 各 unknown key の候補が全て did_you_mean に含まれる (順不同)
        assert "priority" in err.did_you_mean, (
            f"did_you_mean must include 'priority' for 'priorty'; got {err.did_you_mean!r}"
        )
        assert "status" in err.did_you_mean, (
            f"did_you_mean must include 'status' for 'statu'; got {err.did_you_mean!r}"
        )

    def test_multi_key_did_you_mean_empty_when_no_candidates(self) -> None:
        """multi-key で全 key に候補なしの場合、did_you_mean は空のまま."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"foo": "a", "bar": "b"},
                known_keys=["priority"],
            )
        err = exc.value
        # 候補なしキーのみ → did_you_mean は空
        assert err.did_you_mean == (), (
            f"did_you_mean must be empty when no candidates; got {err.did_you_mean!r}"
        )

    def test_multi_key_no_close_match_annotated_explicitly(self) -> None:
        """候補なしキーは message で明示的に "no close match" と示される (B3).

        混在ケース (``'foo'`` 候補なし、``'priorty'`` 候補 ``priority``) で
        括弧が前キーにかかって見えて agent が誤読する懸念を解消。
        """
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"xyzqqq": "a", "priorty": "5"},
                known_keys=["priority", "status"],
            )
        msg = str(exc.value)
        assert "no close match" in msg, (
            f"no-close-match key must be annotated explicitly; got {msg!r}"
        )
        # priorty の候補は従来通り括弧付きで示される
        assert "did you mean: priority" in msg, f"candidate must remain visible; got {msg!r}"

    def test_multi_key_schema_reference_mentions_frontmatter_keys(self) -> None:
        """multi-key message でも ``frontmatter_keys`` リソース名を明示する (B5).

        単一 key は "for the frontmatter_keys list" で具体的だが、multi-key が
        "for the full list" だと agent が schema://tools のどこを見るべきか
        判断しづらい。文言を統一して navigation 先を明示する。
        """
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter(
                {"typo1": "a", "typo2": "b"},
                known_keys=["priority", "status"],
            )
        msg = str(exc.value)
        assert "frontmatter_keys" in msg, (
            f"multi-key message must reference 'frontmatter_keys' for navigation; got {msg!r}"
        )


# ---------------------------------------------------------------------------
# Range operator alias detection (Issue #87)
#
# gt / lt / gte / lte / > / < / >= / <= / greater_than / less_than 等の
# 数値・日付 range 比較 alias を検知し、agent に「未対応であり、
# クライアント側で post-filter せよ」と明示する。
#
# Red フェーズ: filter.py が未対応 alias に対して
# error_code="UNSUPPORTED_RANGE_OPERATOR" と hint を返すよう
# 実装される前の失敗テスト。
# ---------------------------------------------------------------------------

_RANGE_ALIASES = [
    "gt",
    "lt",
    "gte",
    "lte",
    ">",
    "<",
    ">=",
    "<=",
    "greater_than",
    "less_than",
    "greater_than_or_equal",
    "less_than_or_equal",
    # Issue #121: natural-language aliases LLMs tend to generate
    "after",
    "before",
    "between",
    "range",
    "from",
    "to",
]


class TestRangeOperatorAliasDetection:
    """range 比較 alias に対する ValidationError の構造的属性を検証する (Issue #87).

    Red フェーズ: 以下の未実装振る舞いが FAIL することを確認する。
    - 18 alias すべてで error_code="UNSUPPORTED_RANGE_OPERATOR" が送出される
    - hint が non-None で "post-filter" または "client" を含む
    - str(err) に問題のキー名と alias 名が含まれる
    """

    @pytest.mark.parametrize("alias", _RANGE_ALIASES)
    def test_range_alias_raises_unsupported_range_operator(self, alias: str) -> None:
        """range alias で UNSUPPORTED_RANGE_OPERATOR error_code の ValidationError が送出される."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"priority": {alias: "3"}})
        err = exc.value
        assert err.error_code == "UNSUPPORTED_RANGE_OPERATOR", (
            f"alias={alias!r}: expected error_code='UNSUPPORTED_RANGE_OPERATOR', "
            f"got {err.error_code!r}"
        )

    @pytest.mark.parametrize("alias", _RANGE_ALIASES)
    def test_range_alias_hint_mentions_post_filter_or_client(self, alias: str) -> None:
        """range alias のエラー hint に 'post-filter' または 'client' が含まれる."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"priority": {alias: "3"}})
        err = exc.value
        assert err.hint is not None, (
            f"alias={alias!r}: hint must be non-None to guide agent self-correction"
        )
        hint_lower = err.hint.lower()
        assert "post-filter" in hint_lower or "client" in hint_lower, (
            f"alias={alias!r}: hint {err.hint!r} must mention 'post-filter' or 'client'"
        )

    @pytest.mark.parametrize("alias", _RANGE_ALIASES)
    def test_range_alias_message_contains_key_and_alias(self, alias: str) -> None:
        """range alias のエラーメッセージにキー名 'priority' と alias 名が含まれる."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"priority": {alias: "3"}})
        msg = str(exc.value)
        assert "priority" in msg, (
            f"alias={alias!r}: message {msg!r} must contain key name 'priority'"
        )
        assert alias in msg, f"alias={alias!r}: message {msg!r} must contain alias name {alias!r}"

    # -----------------------------------------------------------------------
    # Regression guard: generic unknown operator path は壊さない
    # -----------------------------------------------------------------------

    def test_bogus_operator_keeps_generic_error_code(self) -> None:
        """完全に未知の演算子 'bogus' は UNSUPPORTED_RANGE_OPERATOR ではない generic error."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"foo": {"bogus": "v"}})
        err = exc.value
        assert err.error_code != "UNSUPPORTED_RANGE_OPERATOR", (
            f"'bogus' should not be classified as a range operator alias; "
            f"got error_code={err.error_code!r}"
        )

    def test_bogus_operator_message_contains_unsupported(self) -> None:
        """完全に未知の演算子 'bogus' は従来通り 'Unsupported operator' メッセージを含む."""
        with pytest.raises(ValidationError) as exc:
            parse_metadata_filter({"foo": {"bogus": "v"}})
        msg = str(exc.value)
        assert "Unsupported operator" in msg or "unsupported" in msg.lower(), (
            f"'bogus' operator message {msg!r} must mention unsupported operator"
        )

    def test_in_operator_is_not_range_alias(self) -> None:
        """'in' 演算子は range alias ではなく、正常に MetadataCondition を返す."""
        result = parse_metadata_filter({"priority": {"in": ["a"]}})
        assert len(result) == 1
        assert result[0].op == "in"

    def test_ne_operator_is_not_range_alias(self) -> None:
        """'ne' 演算子は range alias ではなく、正常に MetadataCondition を返す."""
        result = parse_metadata_filter({"priority": {"ne": "a"}})
        assert len(result) == 1
        assert result[0].op == "ne"
