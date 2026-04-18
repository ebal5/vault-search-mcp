---
name: teammate-spawn
description: |
  `TeamCreate` + 複数 `Agent(isolation="worktree")` による parallel PDCA を
  確立手順でセットアップする。**用途は「同一タスクの別側面」に限定**
  (review-loop / 同一 issue の Red/Green 分離 / 機械的 broadcast 等)。
  別 issue / 別 PR 前提の並列化は 2026-04-17 に運用方針で非推奨化済 (4 試行で
  構造的破綻を確認、`.claude/rules/tdd-workflow.md` §「方針: 別タスク並列に
  agent team を使わない」参照)。

  以下の依頼時に使用:
  - 「teammate 並列で ...」「team で並行作業」かつ **同一タスクの別側面**
  - 同一 issue の TDD 分離 (Red/Green/Refactor 評価)
  - 同一 PR 内の parallel review (review-loop)
  - 機械的 edit の broadcast (docstring 多数ファイル等、衝突マトリクス低)

  以下では使用しない (代案あり):
  - 別 issue / 別 PR 前提の並列化 → **別 Claude Code セッション + 各セッション
    内で `EnterWorktree`** (本 skill 冒頭の「⚠️ 警告」参照)
  - 1 issue だけの作業 → 親直接 + worktree 逐次切替 (Baseline) で十分
  - 広範囲 edit (>500 行、>1 ファイル横断) → 親直接 (§tdd-workflow.md
    既知の落とし穴参照)
  - 同ファイル衝突が不可避な issue 組 → Phase を切って逐次化
allowed-tools: Agent, TeamCreate, TeamDelete, SendMessage, TaskCreate, TaskUpdate, Bash(gh pr:*), Bash(gh issue:*), Bash(git:*)
model: sonnet
---

# Teammate Spawn

`TeamCreate` + 複数 `Agent(isolation="worktree")` の parallel PDCA を
確立手順でセットアップするための skill。

> Note: 本リポジトリでは `.claude/skills/` が sandbox 保護で書込制限あり。
> 書込が必要な場合 `dangerouslyDisableSandbox: true` で実行する。

## ⚠️ 警告: 別タスク並列は非推奨 (2026-04-17 方針確立)

**本 skill の適用範囲は「同一タスクの別側面」に限定**。別 issue / 別 PR を
複数 teammate に割り振る運用は、4 試行 (2026-04-16〜17) で以下 4 種の
構造的 pitfall が連鎖発生し、運用対効果が薄いと確認された:

1. teammate `isolation="worktree"` が無視され主リポで作業するケース
2. teammate 間で branch ref 空間が共有され、誤操作 1 回で他 teammate 全滅
3. 親 worktree が PR merge `--delete-branch` で破壊 + local main 汚染
4. 529 overloaded で teammate が PR 作成直前に停止

詳細は `.claude/rules/tdd-workflow.md` の以下セクションを参照:

- §「複数 issue の並列運用パターン」第 4 試行結果
- §「方針: 別タスク並列に agent team を使わない」

### 適合用途 (本 skill を使う)

- 同一 PR 内の parallel review (`review-loop` skill から呼ばれる)
- 同一 issue の TDD 分離 (Red delegate / Green delegate / Refactor 計画評価)
- 調査 + 実装 + テストの役割分担 (同一 issue 内)
- 機械的 edit の broadcast (docstring / annotation 付与等、衝突マトリクス低)

### 非適合用途 (本 skill を使わない、代案あり)

- 別 issue / 別 PR 前提の並列化
  → **別 Claude Code セッションを人間側で複数起動**し、各セッションで親自身
  が `EnterWorktree` する方式を推奨。サンドボックス cwd が worktree に固定
  されるので主リポ汚染リスクなし、PR merge / local main 復旧が各セッション
  内で完結
- 広範囲 edit の分散 (>500 行、>1 ファイル横断)
  → 親直接 + 単一 worktree
- teammate 同士の依存が見えにくい複合タスク
  → 設計分解して逐次化

非適合用途で「どうしても agent team を使いたい」場合でも、本 skill は実行
プロセスを保証するのみで、構造的 pitfall (#1〜#4) の発生確率は下げられない
ことを認識した上で慎重に進めること。

## 前提

- `.claude/rules/tdd-workflow.md` の §「複数 issue の並列運用パターン」を
  一読していること
- 2-4 件の issue が手元にあり、**衝突マトリクス分析済み** (どの teammate が
  何ファイルを触るか解析して、同ファイル衝突がないか確認済み)
- 各 teammate の scope が SMALL〜MEDIUM (1 PR 3〜5 ファイル、~10〜20 分)

## プロセス

### Phase 0: 衝突マトリクス作成 (**必須**)

| teammate | 担当 issue | 触るファイル | 触らないファイル (禁止) |
|---|---|---|---|
| A | #NN | src/x.py, tests/test_x.py | src/y.py |
| B | #MM | src/y.py, tests/test_y.py | src/x.py |

- 同ファイルに両 teammate が触る場合は **Phase を分けて逐次化**
- 異なる関数 / docstring であっても、1 ファイルに並行 edit する場合は
  merge 時衝突リスクあり、事前に判断

### Phase 1: Team 作成

```text
TeamCreate({
  team_name: "phase-x-<topic>-parallel",  // 必ず lowercase
  description: "#NN + #MM 並列 (両方 SMALL)",
  agent_type: "team-lead",
})
```

team_name は lowercase を推奨 (第 1 試行で case 正規化バグ仮説を立てたが、
第 2 試行で否定済。それでも lowercase は副作用なく安全)。

### Phase 2: Task 作成 (記録用、進捗追跡には使わない)

```text
TaskCreate({ subject: "#NN ...", description: "..." })
```

**重要**: teammate は TaskList 不可視 (3 試行で再現)。進捗追跡には使えない。
記録用として残し、teammate prompt には **task description を inline で全文
duplicate** する。

### Phase 3: Teammate spawn (並列)

各 teammate を以下の形式で spawn (全て **並列、1 message で複数 Agent tool
call**):

```text
Agent({
  subagent_type: "general-purpose",  // Edit/Write が必要
  name: "<role>-teammate",           // 識別名、後で SendMessage に使う
  team_name: "<team>",
  isolation: "worktree",             // 独立 git worktree
  model: "sonnet",                   // SMALL〜MEDIUM には十分
  run_in_background: true,           // 親を blocking させない (完了で自動通知)
  description: "...",
  prompt: "<下記テンプレート>",
})
```

### Phase 4: Teammate 完了メッセージ受信

teammate 完了時、system が自動で完了メッセージを delivery する。親は:

1. `gh pr view <N> --json headRefName,commits,files` で **3 点検証**:
   - `headRefName` が prompt で指定した branch と一致
   - `commits` の prefix (Red/Green/Refactor/Docs/Test) が期待通り
   - `files` が scope 外ファイルを含まない
2. `gh pr diff <N>` で内容の最終確認
3. CI が全て pass しているか `gh pr view <N> --json statusCheckRollup`

### Phase 5: Merge

- 先 merge → 後 rebase → 後 merge の順 (第 2 試行プロトコル)
- rebase 必要時: `git rebase --empty=drop origin/main` で重複 commit drop

### Phase 6: Shutdown + TeamDelete

1. 各 teammate に `SendMessage({type: "shutdown_request"})`
2. teammate が `shutdown_response({approve: true})` を返して terminate
3. 全 terminated 後に `TeamDelete()`

## Teammate prompt テンプレート

以下を **scope 固有部分を埋めてそのまま teammate prompt に渡す**。
各セクションを削除・省略しない。

```markdown
<リポジトリ名> の open issue #<番号> を解消する。team `<team-name>` の
teammate として動作する。

## Scope (#<番号>: <タイトル>)

<issue 本体の要約と採用案>

### 具体的な作業

1. <具体 step>
2. <具体 step>
3. <具体 step>

## 必ず従うルール (.claude/rules/tdd-workflow.md 準拠)

- **commit prefix**:
  - 該当するものを選ぶ: Red / Green / Refactor / Docs / Test / Simplify / Fix
  - 挙動変更なら Red → Green → Refactor の 3 commit 独立
  - Refactor 省略可否は §「Refactor 省略の判断基準」を参照
- **禁止事項**: Red で src/ 変更禁止、Green で過剰リファクタ禁止、
  既存テスト意図変更禁止
- **完了条件**:
  - `.venv/bin/pytest` 全 pass
  - `.venv/bin/ruff check src/ tests/` clean
  - `.venv/bin/ruff format src/ tests/` clean
  - PR 作成、CI 全 pass
  - 自分の PR の `gh pr view <N> --json headRefName,commits,files` で
    branch / commits / files が期待通りか自己検証
- **中断条件**: <scope 固有の中断条件を列挙>。該当したら停止して
  team-lead に SendMessage で報告

## 環境セットアップの注意

- sandbox は `.git/index.lock` 等への書込みを拒否する。`git commit` /
  `git push` / `git switch` / `git pull` / `git branch -D` 等は
  **`dangerouslyDisableSandbox: true`** で実行
- 新 worktree の `.venv` が壊れている (main clone の stale pth を指す) ことが
  ある。初回 `.venv/bin/pytest` が `ModuleNotFoundError: vault_search` で死んだ
  ら、`rm .venv && uv sync --all-extras` (これも `dangerouslyDisableSandbox:
  true` 要) で worktree-local venv を作り直す
- `uv run` は sandbox で `~/.cache/uv` への書込みが拒否される。
  `.venv/bin/pytest` / `.venv/bin/ruff` を直接呼ぶ

## PR 作成・報告 (**重要 — 第 3 試行の pitfall 対策**)

- ブランチ名: `<期待する branch>`
- **`gh pr create` 直前に必ず実行**: `git branch --show-current` で
  自分のブランチが `<期待する branch>` と一致することを確認
- **不一致なら**: `git switch <期待する branch>` してから再試行
  (並行 teammate の branch に誤切替が観測されたことがある — 自分では
  気付きにくい)
- PR title: `<Red/Green/Refactor 等 prefix>: <概要> (#<番号>)`
- PR body: 採用案・rationale、完了条件の対応 ✓、test plan を含める
- PR 作成後、CI が全 pass することを確認
  (`gh pr view <N> --json statusCheckRollup`)
- 作業完了 / CI green 確認後、**team-lead に SendMessage で PR 番号と CI
  status を報告**
- team-lead から `shutdown_request` が来たら
  `shutdown_response({approve: true})` で自発終了

## 参照ドキュメント

- `.claude/rules/tdd-workflow.md` — TDD workflow / teammate 並列運用
- `.claude/rules/fastmcp-gotchas.md` — FastMCP の落とし穴 (必要時)
- `CLAUDE.md` — dev commands / conventions

## 禁止 (重要、worktree 衝突対策)

- **<他 teammate が触るファイル>** に絶対に触らない (並列 teammate 作業中、
  worktree base 汚染が観測されたことあり)
- <その他 scope 外制約>
- TaskList / TaskUpdate が "No tasks found" を返す既知バグがある
  (無視してよい、報告は SendMessage で)

よろしく。
```

## 既知の pitfall (試行で観測済)

- **TaskList 不可視** (1-3 試行全て): team_name の case 問わず teammate から
  TaskList が空を返す。**回避**: task description を prompt に inline
- **worktree base 汚染** (第 2 試行): 先に commit した teammate の HEAD が
  second worktree の base に流入して duplicate commit が混ざる。
  **回避**: 親が `gh pr diff` で scope 外 commit を検出、`git rebase
  --empty=drop origin/main` で drop
- **PR 作成 branch mismatch** (第 3 試行): teammate が並行 teammate の branch
  上で `gh pr create` して title/contents mismatch な PR が作成される。
  **回避**: prompt に `git branch --show-current` 確認を明示 + 親が
  `gh pr view --json headRefName` で 3 点検証
- **team-lead git 操作も sandbox 抵触**: `git switch` / `git pull` /
  `git branch -D` が `.git/index.lock` 書込みで失敗。
  **回避**: `dangerouslyDisableSandbox: true` を常用

## 失敗時の fallback

timeout / permission 拒否 / conflict 暴発が発生したら、その時点で
teammate を shutdown → 親セッションの worktree 逐次切替 (Baseline) に戻す。
test/commit のロールバックは各 worktree 独立なので影響局所化済。
