---
name: review-loop
description: |
  複数視点の fresh-context reviewer を並列起動し、スコア 0-10 で triage、
  スコア閾値に応じて「対応」または「Issue 起票」を行い、新規 5+ 指摘が
  出なくなるまでループする批判的レビュープロセス。

  以下の依頼時に使用:
  - 「レビューして」「レビューラウンド」「チームレビュー」
  - 「5 以上がなくなるまで」「収束するまでレビュー」
  - 「外部視点で批判的に評価して」「独立したレビュアーで」
  - 機能実装完了後の最終品質検証

  以下では使用しない:
  - 単純な bugfix 後の軽い確認 → 通常の /simplify で十分
  - 実装前の設計議論 → /grill-me が適切
allowed-tools: Agent, Bash(gh issue:*), Bash(gh label list:*), Bash(gh pr list:*), Bash(gh pr view:*), Bash(git log:*), Bash(git diff:*), Bash(git status:*), Bash(git branch:*), TaskCreate, TaskUpdate, TaskList, TaskGet, Read, Write, Edit, Glob, Grep
model: sonnet
---

# Review Loop

複数視点の fresh-context reviewer を並列起動し、スコア閾値で triage、
新規 5+ 指摘が出なくなるまで収束させるレビュープロセス。

> Note: 本リポジトリでは `.claude/skills/` が sandbox 保護で書込不可のため、
> 暫定的に `.claude/review-loop/` に配置。将来 `.claude/skills/review-loop/`
> に移動可能。

## 前提

- git ブランチでの作業 (main と比較して diff が取れる)
- GitHub リポジトリ (Issue 起票先、`gh` CLI が使える)
- 最低限のテスト・lint がパスしている状態
- `.claude/settings.json` の allow に `gh issue create:*` が入っている

## プロセス

### Phase 0: 前提確認

1. 現在のブランチ、`git log --oneline main..HEAD | head -20`、テスト状態を把握
2. 既存 open Issue を一覧 (`gh issue list --state open --limit 60 --json number,title`)
3. ユーザーに方針確認 (閾値、視点の絞り込み、最大ラウンド数など)

### Phase 1: 並列レビュー

**4 視点を並列起動** (独立した Agent として、**既存コードを追認させない**):

| 視点 | 焦点 | モデル |
|---|---|---|
| **A: Correctness** | バグ、エッジケース、未検証の仮定、race、leak | Sonnet |
| **B: Agent DX** | MCP/CLI エージェントが初見で誤用する箇所、schema の機械可読性 | Sonnet |
| **C: Test Quality** | t-wada スタイル、vacuous pass、brittle、実装ミラー、実質網羅性 | Sonnet |
| **D: Architecture** | god class、責務漏れ、結合度、拡張性、層越え | **Opus** |

モデル指定は `Agent({subagent_type: "general-purpose", model: "sonnet" | "opus", ...})`
で明示する。Architecture 視点 (D) だけ Opus を残す根拠は、層越えや結合度の
判断が 1 段抽象的で、Sonnet では見落としやすいため。コスト削減効果は 4 視点
ラウンドで Opus 4→1 (75% 削減)。

各 Reviewer へのプロンプトに**必ず含める**:

- **既知 Issue のタイトル一覧** (重複排除)
- **スコア基準** (10=クラッシュ、8-9=確実発火、6-7=改善余地大、5=小実害、<5=省略)
- 「既存を追認しない」指示 ("動いている" "Issue 化済み" は理由にならない)
- **出力フォーマット** (場所/スコア/問題/推奨/既存 Issue 独立性)
- **最大指摘数 5-7 件で抑制** (本当に重要な順)

### Phase 2: トリアージ

Reviewer 報告を集約し、スコアで振り分け:

| スコア | 対応 |
|---|---|
| **≥8** | 即 FIX (コード修正 + TDD) |
| **5-7** | Issue 起票 OR FIX (ユーザー判断 / デフォルトは Issue) |
| **< 5** | 無視 |

### Phase 3: 対応実行

- **FIX**: サブエージェントに delegate、TDD 必須 (Red → Green → Refactor 各 commit)
  - Red / Green / Refactor の **実施** は Sonnet delegate
  - **Refactor プランの評価**(実施前の設計レビュー) は Opus で別 Agent に回す
    — 計画の盲点・副作用検知で Opus の抽象思考が効く。プラン自体の作成は
    Sonnet で十分
- **Issue 起票**: `gh issue create --label enhancement --body-file <body>` で一括
  - Issue body は「背景 / 再現 / 推奨修正 / スコア / 関連 Issue」構成
  - 事前に `.tmp-issues/*.md` に本文生成しておくと retry 容易

### Phase 4: 収束判定

修正/起票完了後、再び Phase 1 (別の fresh-context Agent で) を実行。

- **新規 5+ 指摘 0 件 → 収束、終了**
- **新規 5+ 指摘あり → Phase 1 に戻る**
  (既知 Issue を Reviewer に伝えて重複排除)
- Reviewer 自身に「収束判定」を求めるのも有効 (diminishing returns を検知させる)

## ループ上限

無限ループ防止のため:

- **Max 8-10 ラウンド**で打ち切り、残項目は全て Issue 起票して終了
- **Round N fix が Round N+1 で regression 指摘される連鎖**に入ったら、
  fix 前の設計を再検討
- ユーザーに中間報告 (各ラウンド完了時に進捗と次の方針)

## レビュー中のスクリプト実行

reviewer または fix エージェントが動作確認のため一時スクリプトを書いて
実行したいケース (MCP の wrap 挙動検証、SQL 生成確認、schema 比較など)。
**実行前に必ず `execute-script-safely` sub-skill で事前レビューを通す**。

詳細は `.claude/execute-script-safely/SKILL.md`。要点:

- haiku Agent で 7 項目チェック (process spawn / FS write / secret read /
  network / env exfil / dynamic exec / obfuscation)
- `clean` 判定のみ実行許可、`suspicious` / `dangerous` はユーザー確認

## アンチパターン

- **Reviewer に前ラウンドの findings を渡してしまう** → 追認が起きて独立性喪失
- **スコア基準を甘くする** → 低スコアがノイズ化、重要事項が埋もれる
- **Issue 起票せず Fix だけで進める** → 残タスクの追跡困難、後続セッションに引き継げない
- **Reviewer を 1 視点しか起動しない** → 偏った指摘、複数視点で補完が必要
- **FIX を Refactor 省略で済ませる** → Green の妥協が次ラウンドで批判される連鎖の原因
- **無制限ループ** → 8 ラウンド超えたら diminishing returns の合図

## プロジェクト実績 (vault-search-mcp, 2026-04-14)

- 9 ラウンド実施、9 critical FIX + 42 Issue 起票 (#14-#61)
- Round 4-6 は前 Round fix の meta-critique 連鎖 (FastMCP API 制約起因)
- Round 9 で「実質収束、実装フェーズ移行推奨」と reviewer 判定
- FastMCP の Union 戻り型 / wrap_output / outputSchema 制約への迂回で
  workaround コードが 3 層累積 → 根本解は上流 SDK に依存

## プロジェクト固有メモの拡張

プロジェクトごとの conventions (特定 SDK の罠、命名規則、test infrastructure
の癖など) はこの SKILL.md 末尾ではなく、プロジェクトの `CLAUDE.md` に記載
するのが望ましい。スキル起動時に `CLAUDE.md` を読んでプロジェクト固有の
配慮事項を踏まえる。
