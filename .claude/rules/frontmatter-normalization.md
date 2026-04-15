# Frontmatter 正規化の設計判断 (Issue #15 / #49)

## 採用方針

**index-time に frontmatter スカラーを文字列へ正規化する** (parser.py の
`_normalize_fm` / `_normalize_scalar`)。型情報は保持しない。

- bool → `"true"` / `"false"` (YAML 表記と一致)
- int / float → `str(v)` (`5` → `"5"`、`4.5` → `"4.5"`)
- date / datetime → `isoformat()` (`2024-01-15` / `2024-01-15T14:30:00`)
- None / str / list / dict → 再帰的に適用 (None と str は素通し)

`filter.py` の SQL は str→str の素の等価比較のみを扱う。

## 代替案と trade-off

### Option A (採用): index-time 正規化 + 型情報喪失

- **pros**: SQL 層が単純 (CAST 不要、bool `"1"`/`"0"` ワートなし)。
  schema description も「全て文字列」と言い切れる。agent DX も一貫。
- **cons**:
  - `vault_get_note` が YAML 原文の型を返さない (`priority: 5` → `"5"`)。
    round-trip 用途 (export / 書き戻し) には不向き。
  - 将来 #11 (frontmatter_kv 側テーブル + typed 比較) を実装する際、
    原型情報を取り戻すには YAML ファイルの再パースが必要。
  - `NoteDetail.frontmatter` の description は「正規化済み」と明示する
    必要がある (そうしないと agent が型判定を誤る)。

### Option B (不採用): 二重列保持

`notes` テーブルに `frontmatter`(raw JSON) と `frontmatter_norm`(文字列化)
を両立させる案。

- **pros**: `vault_get_note` は原型返却、SQL filter は str 比較。
  #11 も raw 列を使える。
- **cons**:
  - INSERT/UPDATE で 2 列同期、インデックスサイズ倍増。
  - 正規化不整合 (片方だけ stale) の新規バグクラス。
  - 現段階で typed 比較の具体要件がない (YAGNI)。
- **いつ再検討するか**: #11 が実装段階に入り typed comparison が必須要件に
  なった時、または `vault_export` / `vault_update_frontmatter` 的な
  round-trip 系機能を追加する時。

### Option C (不採用): query-time CAST

`filter.py` の SQL に `CAST(json_extract(...) AS TEXT)` を挿入する案
(本 PR の初期実装)。

- **pros**: 実装局所、parser を変えない。
- **cons**:
  - SQLite JSON1 が bool を 1/0 で返すため、`"true"`/`"false"` で
    マッチしない UX ワート。schema description に恒久的 caveat。
  - CAST 6 箇所の重複 (#29 の coupling 悪化)。
- **再採用するケース**: ほぼない。index-time 正規化より情報量は多いが、
  bool ワートを根治できないので DX 的に劣る。

## Trust boundary

`ParsedNote.__post_init__` で `_normalize_fm` を自動適用する。

- `parse_note` 以外の経路で `ParsedNote(...)` を直接構築しても str→str
  不変条件が壊れない。
- `_normalize_scalar` は str を素通しするため冪等 (二重適用でも安全)。
- `frontmatter_json` は `default=str` を持たない。非 JSON 値が流入したら
  `TypeError` で即座に surface する (silent coercion を許さない)。

## 既知の残課題 (本 PR 外の follow-up)

- **D8 / #XX**: 型情報の非可逆喪失は #11 の前提を壊す。side table 実装時に
  原 YAML 再パースか、Option B への移行を再評価。
- **B-R2-3 / #XX**: 数値・日付の範囲比較 (`gt`/`lt`/`gte`) は未対応。
  エージェント向け description に明記済みだが、`metadata_filter` の
  operator 拡張 (#13 と合わせて) を将来検討。
- **B-R2-5 / #XX**: YAML null / 欠落キーの filter は不到達
  (eq / ne どちらにもマッチしない)。`{"key": {"exists": false}}` 的な
  operator 追加で解消可能。
- **datetime の timezone**: `Z` suffix は PyYAML が UTC を `+00:00` で
  正規化するため、agent が `"...Z"` で filter すると silent miss。
  description に明記済み。
