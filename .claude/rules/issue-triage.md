# Issue Triage / Close 規約

GitHub issue の大量トリアージとクローズ判断は subagent (Opus など) に委譲して
よいが、**close の実行前に親 Claude が個別検証する**こと。triage 結果を
無批判にコマンド実行するのは禁止。

## 背景

2026-04-15 のトリアージで、Opus agent が #32 (`ValidationError` 継承) を
「解消済み」と判定し close 推奨したが、実コードは `class ValidationError(ValueError)`
のみ残っており、issue が求めた `VaultSearchError` 階層分離は **未実装** だった
(HANDOVER の B1-2 で YAGNI 明示 defer 済み)。subagent は docstring 周辺の解消に
気を取られ、issue 本文の完了条件を誤読した。

## トリアージ手順

### Phase 1: subagent に一次分類させる

Opus agent を general-purpose で起動 (model 明示、`.claude/rules/delegation.md` 準拠):

- open issue を `gh issue list --json number,title,labels,body` で取得
- 各 issue を現状コードに照らして `CLOSE_STALE` / `CLOSE_WONT_FIX` / `SMALL` /
  `MEDIUM` / `LARGE` に分類
- close 候補には「なぜ解消されたか」の具体的根拠 (PR 番号 / ファイル:行番号) を
  要求
- 出力形式は「分類 + 根拠 + close コメント案」

### Phase 2: 親が close 前に個別検証

親 Claude は **close 推奨の 1 件 1 件について** 以下を実行:

1. `gh issue view <N> --json body -q .body` で issue 本文を読む
2. 本文の **"## 完了条件"** (または相当セクション) を直接抽出
3. 完了条件の各項目を現状コード (`src/` / `tests/`) に対して確認
4. **完了条件が満たされていない項目が 1 つでもあれば close しない** —
   subagent 判定を差し戻す

盲点パターン:

- subagent が issue の「背景」や「再現例」だけ読み、「完了条件」を読み飛ばす
- docstring や comment レベルの解消を、設計変更を求める issue の解消と
  混同する
- 関連 PR が複数 issue を一括解消したように見えるが、実は副次的な部分だけ
  触っていた

### Phase 3: close 実行

検証を通った issue のみ `gh issue close <N> --comment "..."` で閉じる。close
コメントには:

- どの PR / commit / ファイル箇所で解消されたか
- 完了条件のどの項目が満たされたか
- 満たされなかった項目があれば、その部分を引き継ぐ別 issue 番号

を含める。後から「なぜ close したか」を辿れるようにする。

## 失敗時のリカバリ

誤って close した issue は `gh issue reopen <N> --comment "<理由>"` で復活可。
trace が残るので過度に保守的になる必要はないが、再 open 時はコメントで経緯を
残すこと。

## close せず残す判断例

以下のような issue は解消の「根拠が弱い」ため close せず残す:

- **YAGNI defer されている構造変更**: `except ValidationError:` catcher 要件が
  発生したら 2 行で実装できるが、現状は不要で defer 済 — open のまま置いて
  catcher 発生時に実装する
- **部分解消**: 完了条件 5 項目中 3 項目だけ満たされた状態。残 2 項目を別
  issue に切り出して元 issue は残すか、完了条件を追記して明示する
- **実害が観測されていない設計懸念**: drift 可能性があるが drift 実例ゼロの
  ケース。close して「実害発生時に reopen」の運用でもよいが、その方針を
  close コメントに明記する

## コマンド早見表

```bash
# open issue 一覧 (triage 入力用)
gh issue list --state open --limit 50 --json number,title,labels,body > /tmp/issues.json

# 個別 issue 本文確認 (close 前検証用)
gh issue view <N> --json title,body -q '.title, .body'

# close 実行
gh issue close <N> --comment "..."

# close 取消
gh issue reopen <N> --comment "..."
```

`/tmp/issues.json` のような一時ファイルはリポジトリルートに置かない (過去に
`.issues.json` をルートに作って gitignore に追加する雑務が発生した)。
