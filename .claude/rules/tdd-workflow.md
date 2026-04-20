# TDD ワークフロー (実験中)

本リポジトリは SDLC を段階的に厳格化する実験中。以下を徹底する。

## Red → Green → Refactor を独立 commit にする

- **Red**: 失敗テスト追加のみ (実装変更禁止)
- **Green**: テストを通す最小実装 (過剰実装・リファクタ禁止)
- **Refactor**: 妥協解消 + 可読性向上 (機能追加禁止、テスト全通維持)

## Refactor フェーズを省略しない

Green で入った応急措置を Refactor で本来の設計に戻す重要タイミング。観察された
実例:

- `default=""` 付与による必須フィールドの optional 化 (Pydantic 構築のため) →
  Refactor で `model_construct` に変更して復元
- local import (循環回避の応急処置) → Refactor で依存関係を整理し top-level 化
- 重複ロジック (ツール間コピペ) → Refactor で共通ヘルパに抽出
- 過剰な defensive コード (YAGNI 方向) → Refactor で削除

Refactor は 1 commit に詰め込まず、**論点ごとに別コミット**に分けると diff
レビューが容易。

### Refactor 省略の判断基準

「応急措置を放置しない」が spirit なので、Green が既に minimal で housekeeping
ゼロなら無理に Refactor を作らず PR body に省略理由を明示する。観察された
判定例:

- **省略した例** (#119, PR #132): Green は 2 ファイル <20 行、
  `known_keys` パラメータを削除して indexer 内部で自己解決するだけの
  最小変更。応急措置 (default 付与 / local import / 重複) ゼロ → 省略
- **実施した例** (#118, PR #134): Green で 3 書込み経路に
  `_cache.invalidate()` と `self._frontmatter_keys_cache = None` の 2 行
  ペアが重複。将来 cache 種類が増えた際の同期漏れリスクが明確 →
  `_invalidate_caches()` helper に集約する Refactor を実施

判定の軸は「Green diff に **repetition / 応急回避 / stale comment** が
混入しているか」。混入があれば論点ごとに Refactor commit を切る。

## サブエージェントへの delegate

CLAUDE.md の AI Agent Development Policy に従い、実装はサブエージェントへ
delegate する。各フェーズで以下を明示:

- **対象フェーズ**を明示 (Red のみ / Green のみ / Refactor のみ)
- **禁止事項**を明記
  - Red 時: src/ 変更禁止、既存テスト改変禁止
  - Green 時: 過剰リファクタ禁止、テスト改変禁止
  - Refactor 時: 機能追加禁止、テスト意図変更禁止
- **完了条件**を明記 (テスト通過数、commit 形式、lint クリーン)
- **中断条件**を記述 (これに該当したら停止して報告)

### モデル指定指針

コスト最適化のため、フェーズ / 用途ごとに `Agent({model: ...})` で明示指定する
(Agent tool の `model` param は agent definition frontmatter を override する)。

| 用途 | モデル | 根拠 |
|---|---|---|
| Red delegate (失敗テスト追加) | Sonnet | テスト骨格組立に十分 |
| Green delegate (最小実装) | Sonnet | 単機能実装に十分 |
| Refactor **計画作成** | Sonnet | 機械的な整理案の列挙 |
| Refactor **計画評価** (実施前) | **Opus** | 盲点・副作用・設計影響の抽象思考が効く |
| Refactor delegate (実施) | Sonnet | プラン追従の実装は Sonnet 十分 |
| 一時スクリプト security pre-review | Haiku | `execute-script-safely` 規定通り |

Refactor は「計画 (Sonnet) → 評価 (Opus) → 実施 (Sonnet)」の 3 段を推奨。
評価ステップを Opus に分離することで、計画段階で Opus を長時間走らせる無駄を
避けつつ、危険な refactor を実施前に止められる。

**Opus 評価は NO-GO を返せる権限がある**: 計画を無批判に追認しない。
blocker が見つかれば修正版骨子ごと差し戻す。親は Opus の指摘を受けて
計画を差し替えてから実施に進む。Sonnet 計画に reflexive なバイアス
(「後方互換を維持するため re-export を残す」「実装詳細に tests が依存
する形で inline する」等) が混入しやすく、Opus がこれを捕捉した実績
あり (2026-04、Phase B1 PR #96)。

### 既知の落とし穴: background subagent は Edit/Write が拒否される

`Agent(..., run_in_background=true, mode="acceptEdits")` および
`mode="bypassPermissions"` は現行 harness で honor されず、background subagent
では Edit/Write が恒久的に拒否される (2026-04 時点)。`/permissions` で allow に
追加しても subagent プロセスには伝搬しない。

**回避策**:

- **親セッションが worktree を切替えて逐次実装** (今回の Phase 2 で採用)
- foreground agent (`run_in_background=false`) を使う — 親のインタラクティブ
  permission を経由できる
- Bash ヒアドキュメントで新規ファイル作成する (Edit 不要な初期追加のみ)

サブエージェントが settings.json に Edit/Write を自己追加しようとした事例が
1 件観測されている (prompt 指示なしの self-elevation) — コミット前に必ず
`git diff .claude/settings.json` で確認する。

### 既知の落とし穴: 大規模 edit + test 反復タスクは foreground agent でも timeout

Sonnet foreground agent に「広範囲ファイル編集 + pytest 反復で整合性検証」を
任せると ~90 分で stream idle timeout する実績あり (2026-04、PR #92 の fields
削除委任時。12 tool use で timeout、commit ゼロ、partial edit のみ残存)。

**判断指針**:

- **read-only 調査** (Explore, review, grep/glob 系) → agent OK、安定
- **単一ファイル小規模 edit** (1-50 行、pytest 反復不要) → agent OK
- **広範囲 edit + 整合性検証** (複数ファイル、pytest 反復修正が絡む) → **親直接**
  - 親の Edit tool 経由なら permission 通過が確実
  - 途中状態の `git diff` が見える (timeout 時も partial を引き継げる)
- **複数タスクを agent 並列化したい場合** → 各タスクを小さく切って個別 agent に

timeout した partial edit は `git diff` で状態確認のうえ、親で残作業を完成
させるか revert するか判断する (今回は partial を活かして親で完成)。

### 既知の落とし穴: 新 worktree の .venv が旧 worktree の src/ を指す

`EnterWorktree` で新 worktree を作ると `.venv` は main clone の `.venv` を
シンボリックで参照するが、main の editable install pth
(`.venv/lib/python*/site-packages/_editable_impl_vault_search_mcp.pth`) は
**最後に `uv sync` を走らせた worktree の src/ を絶対パスで指したまま**。
先行 worktree が既に `ExitWorktree(remove)` で削除されていると、後続
worktree で `.venv/bin/pytest` が `ModuleNotFoundError: vault_search` で
conftest import 段階で即死する (2026-04、PR #134 で再現)。

**対処**: 新 worktree 入り直後、初回 test 実行前に worktree-local な
独立 venv を作る:

```bash
# sandbox で ~/.cache/uv 書込みが拒否されるため最初の 1 回だけ
# dangerouslyDisableSandbox: true が必要
rm .venv && uv sync --all-extras
```

以降は `.venv/bin/pytest` / `.venv/bin/ruff` を直接呼ぶ (CLAUDE.md の
worktree 運用記述通り)。

新 worktree のセットアップチェックリストとして、README / TODO 系に
「`.venv` pth の指す先を確認するか、無条件で `rm .venv && uv sync`」を
組み込むのが安全。

### 既知の落とし穴: machine migration 後の working tree 不整合

別マシンで作業履歴のあるセッションを引き継ぐと、working tree が HEAD と
不整合な状態で現れることがある (2026-04-20 PR #209 で観測)。具体例:

- 一部 tracked files が HEAD より**古い** 状態にリバートされている
- 別の tracked files は origin/main の**新しい** commit 分まで進んでいる
- `git status` は一律 `modified` と表示するため、作業中の変更と破損が区別
  できない

**検知手順** (セッション開始時の sanity check):

```bash
git status              # modified 表示の存在を確認
git diff HEAD --stat    # ファイル別の ± 行数で「意図しない変更」を炙り出す
git stash list          # 未処理の作業が stash に退避されていないか
```

3 点セットで「未コミット作業ゼロ」かつ「working tree が HEAD と乖離」が
確認できた場合は破損と判断:

```bash
git checkout -- .       # tracked files を HEAD に合わせてリセット
git rebase origin/main  # ブランチを最新 main に乗せ直す
```

**教訓**: `git status` 単独では不足。`git diff HEAD --stat` + `git stash
list` を組み合わせて「未コミット作業」と「working tree 破損」を区別する
のが必須。破損放置で rebase すると unstaged changes エラーが出て進まない。

## Red/Green vs Test/Refactor — prefix 選択ガイド

commit prefix は「振る舞いが変わるか」で選ぶ:

- 既存バグで実挙動が仕様と合わず、修正で挙動が変わる → **Red → Green**
- 既存挙動は正しいが SDK upgrade 等で silent regression しうる保険を pin する
  → **Test:** (テスト追加のみ、失敗しない) + **Refactor:** (実装整理)
- 例: `vault_reindex` の Pydantic 戻り → dict 戻り統一 (#59, PR #63) は
  現状 FastMCP が flat に再シリアライズするため Red にならない。regression
  guard を `Test:` で先行追加し、dict 化は `Refactor:` で別コミット。

## 並行作業時の調停

複数サブエージェントが src/ / tests/ を同時に触る場合:

- 親 Claude が git stash / rebase を管理 (サブエージェントには委ねない)
- 各エージェントに「他エージェントが同時進行している可能性」を共有
- `git pull --rebase` 相当の自動調停は禁止 (意図しないマージは親の責任)
- 同一ファイルを 2 エージェントが書く予定があれば逐次化、並列しない

## 同一ファイル touch issue の統合 PR ガイドライン

複数 issue が同じファイルを必ず触る場合、個別 PR にすると merge 順序の rebase
コストが累積する。以下の判定で統合 PR を採用する:

### 統合 PR を採用する条件 (AND)

1. **責務を 1 行で言語化できる** (例: 「schema://tools payload の agent DX 改善」)
   — 複数 issue を同じ責務クラスタとして説明できる
2. **同一ファイルを高確率で touch** (例: 全部 `resources.py` の同一 dict)
3. **個別 PR に分けても review が独立しない** — どの issue を先 merge しても
   後続が rebase 必須

### 個別 PR を維持する条件

- 責務が異なる (例: cache 改善 + observability 改善は同じ indexer.py でも別責務)
- 1 issue が他に依存しない、片方だけ revert したい可能性がある
- review reviewer の専門が分かれる (perf / security / DX が混在)

### 統合 PR の commit 粒度

#### パターン A: 統合 TDD (責務が強く結合する場合)

issue ごとに Red/Green/Refactor を分けると 4 issue × 3 commit = 12 commits
となり review が破綻する。**1 つの責務として TDD 構成する**:

- `Red`: 全 issue 統合の失敗テスト追加 (1 commit)
- `Green`: 全 issue 統合の最小実装 (1 commit)
- `Fix Round N`: review-loop 指摘対応 (round 1 commit)

PR body の commit 説明節で「issue → commit hash」の対応を書く。

**適合例**: PR #204 (resources.py の同一 dict に 4 issue 重畳)。テストも
実装も互いに絡み合い、個別 TDD では同じ箇所を何度も書き直す冗長さが出た

#### パターン B: issue 別 commit + 責務言語化 (issue が技術的に独立する場合)

**適合例**: PR #206 (#168/#180/#181/#185/#190 の indexer.py hygiene)。
同ファイル touch だが各 issue が別関数を触り TDD スタイルも様々
(Refactor のみ / Red→Green / Docs のみ)。**個別 commit を保つことで
Red/Green/Refactor prefix 規約と bisect 可能性が維持された**。

- issue ごとに 1-3 commit (自然な TDD フェーズに応じて可変)
- 合計が 10 commit を超える場合は統合 TDD (パターン A) への切替を検討
- PR body で「責務を 1 行で言語化」し、各 issue の解決内容を短く列挙

#### 判定フロー

1. 各 issue の touch 範囲が重なるか確認 (同じ関数 / 同じ dict か)
2. 重なる → パターン A (統合 TDD)
3. 重ならない & 合計 ≤10 commit → パターン B (issue 別 commit)
4. いずれでも合計 >15 commit なら個別 PR に戻すことを検討

### 既知の落とし穴: auto-merge と branch 削除の race (2026-04-20)

`gh pr merge --auto --rebase` 設定後、同 branch に追加 commit を push しようと
したタイミングで auto-merge が先に完走すると、以下の race が起きる:

1. リポジトリ設定で「merge 時に branch 自動削除」が有効だと branch が消える
2. 遅れて届いた `git push` は削除済 branch を新規作成 (`[new branch]` メッセージ)
3. PR は既に **merged** 状態なので、新規 push した commit は PR に反映されない
4. `gh pr view <N>` で `state: MERGED` を確認するまで見逃しやすい

**回避**:

- **auto-merge 設定は review-loop 完了後にする**。review-loop で検出した
  fix を全て同 PR に込めてから auto-merge を設定する
- どうしても merge 後に追加修正が必要な場合は、`origin/main` から fresh
  branch を切って `git cherry-pick <commit>` で移植 → 別 PR 起票
- push 後に `[new branch]` メッセージが出たら「前の PR は merged 済み」の
  signal として疑う

**実例**: PR #206 で auto-merge 設定 → review-loop 起動 → finding 検出 →
Simplify commit push 時に上記 race 発生。fresh branch 切って別 PR #207 で
吸収して復旧。

### 並行 PR で同一 issue が独立実装された場合の rebase 戦略

別セッションが同じ issue を独立実装し先に merge された場合 (PR #204 と #203
の #196 衝突実例):

1. 先着 PR が採用した実装方針を rebase で取り込む (=自分の同 issue 実装は捨てる)
2. 自分の追加要件 (本 PR 例: #192 の optional/condition フィールド) を上書き
   復元する
3. **副次的 drift** に注意: 先着の docstring と自分の implementation 解決が
   独立に rebase されて整合性が崩れる典型シナリオ。rebase 完了後に
   `git diff origin/main..HEAD` で docstring と実装の言及内容が一致するか
   目視確認する

## 複数 issue の並列運用パターン

Phase A/B のように「互いにほぼ独立した 2-4 issue を短時間でまとめて片付けたい」
場合の運用指針。試行結果を蓄積しつつ更新していく (2026-04-17 時点で 4 試行
完了)。

> **2026-04-17 方針転換**: 4 試行目で「別 PR 前提の別タスクを agent team で
> 並列化する運用」は構造的に破綻すると確認した (詳細は本節「第 4 試行結果」
> および「方針: 別タスク並列に agent team を使わない」)。**第一選択は
> Baseline (親 worktree 逐次切替) または別セッション並列**。agent team は
> 「同一タスクの別側面」用途に限定する (review-loop / 同一 issue の Red/Green
> 分離等)。

### Baseline: 親セッション worktree 逐次切替 (実績あり、第一選択)

親 Claude 自身が `EnterWorktree` → Red/Green/Refactor 直接 Edit → PR 作成 →
`ExitWorktree(remove)` → 次 issue 用に新 worktree を作る、を逐次繰り返す。

- **安定性**: 2026-04-16 セッションで #117 / #120 / #119 を ~30 分/件で
  回せた実績あり。timeout / permission 拒否なし
- **向く task**: 1 PR あたり <100 行、TDD 3 commit 程度。filter.py のような
  中核ファイルを触っても親の Edit は permission 通過が確実
- **向かない task**: 1 PR あたり >500 行の広範囲 edit。親が逐次だと時間が
  累積する
- **弱点**: 「並列」ではなく単にシリアライズしているだけ。壁時計時間は
  issue 数に比例

### 初試行済 (2026-04-16): TeamCreate ベースの parallel PDCA

`TeamCreate` で team を作り、親 (team lead) が issue ごとに worktree 付き
teammate を spawn して並列実行する案。**2026-04-16 に #137 (doc 追記のみ) で
1 teammate による初試行実施、PR #144 merge まで完走**。以下に想定フローと
実測結果を記録。

**想定フロー**:

1. 親が `TeamCreate(team_name="phase-x-parallel")` で team を開く
   (team_name は lowercase 推奨 — 後述の TaskList バグ回避)
2. 親が各 issue を `TaskCreate` で team 共通タスクリストに登録 (completion
   条件・禁止事項・lint/test の完了チェック項目を含める) — **ただし teammate
   からは現状 TaskList が見えないので詳細は teammate prompt に inline で重複
   埋め込みする**
3. 各 teammate を `Agent(subagent_type="general-purpose", team_name=...,
   isolation="worktree", model="sonnet")` で spawn。`general-purpose` は
   Edit 可能、`isolation: "worktree"` で独立 git worktree が切られる
4. teammate は Red → Green → Refactor → PR 作成まで完結し `TaskUpdate` で
   完了報告 + `SendMessage({to: "team-lead", ...})` で PR 番号通知
5. 親は teammate からの完了メッセージ (自動配信) を受けて review / merge 判断
6. 親が `SendMessage({to: "teammate-name", message: {type: "shutdown_request"}})`
   で終了を要請、teammate が shutdown_response で approve して自発終了
7. 全 teammate terminated 後 `TeamDelete` で cleanup

**期待される効果**:

- 壁時計時間: 最も遅い teammate 1 件分で済む (真の並列、複数 teammate 時)
- 親コンテキスト節約: teammate が Red/Green/Refactor 往復を吸収
- 設計判断の分担: 親は triage / integration、teammate は実装

**初試行結果** (2026-04-16、`docs-teammate` / #137 / PR #144):

- ✅ **Edit/Write permission**: `Agent(isolation="worktree")` foreground
  teammate で Edit が permission 通過した。`.venv` / markdown のみの修正で
  あれば追加の permission elevation 不要
  (§既知の落とし穴「background subagent は Edit/Write 拒否」は
  worktree isolation + foreground では解消されている)
- ⚠️ **git commit / push の permission**: teammate 内でも親と同様に
  `dangerouslyDisableSandbox: true` が必要。sandbox allow list に
  `.git/index.lock` 等の一時ファイル path が含まれていないため通常
  sandbox では `unable to write new index file` で失敗。teammate prompt に
  「git 操作時は dangerouslyDisableSandbox を使うこと」を明記する
- ✅ **timeout**: markdown 追記のみの最小 scope では発生せず。ただし scope
  を拡大した次回試行 (実装コード触る、pytest 反復する) で改めて timeout
  リスクを確認する必要あり
- ❌ **TaskList / TaskUpdate の team context 解決バグ**: `TeamCreate(team_name=
  "phase-B-137-trial")` の実 config path が `~/.claude/teams/phase-b-137-trial/
  config.json` (lowercase) になり、teammate 内から `TaskList` は "No tasks
  found"、`TaskUpdate({taskId: "1"})` は "Task not found" を返す。原因仮説は
  team_name の case 正規化ミスマッチ

**初試行からの教訓**:

- **team_name は最初から lowercase で作る** (`phase-b-137-trial` 等)。
  TaskList/TaskUpdate の case 解決バグを避けるための暫定 workaround
- **task description を teammate prompt に duplicate する**: TaskList が
  機能しない前提で、scope / 禁止事項 / 完了条件 / 中断条件を teammate
  prompt に inline 埋め込む。TaskCreate は親側のメモとして残すが teammate
  は読めないと見なす
- **scope を極小にしたのは正解**: 設計判断 (workaround 採否等) は親に残し、
  teammate は機械的な文書化のみ担当させた → 迷走せず prompt 熟読 → 実装
  → PR の単線フローで完結。teammate の model は `sonnet` で十分
- **shutdown は request/response で行う**: teammate は idle 状態で
  `shutdown_request` を受け取り `shutdown_response({approve: true})` で
  自発終了する。親が `TeamDelete` する前に必ず shutdown_request を送る

**懸念点 / 残検証事項**:

- **広範囲 edit + pytest 反復での timeout 再現性**: 本試行は doc のみで未
  検証。広範囲 edit では ~90 分 stream idle timeout (§既知の落とし穴) が
  再発する可能性。teammate が timeout した場合、partial edit を親が
  引き継げる設計にしておく (task description に「中断した場合の recovery
  指針」を明記)
- **同ファイル衝突**: 各 teammate が独立 worktree を持つなら conflict は
  merge 時に発生。事前に衝突マトリクスを組み、衝突可能性ある issue は
  同一 team で並走させない (Phase を切る)
- **メッセージの取り回し**: teammate は plain text で報告する。親は
  teammate を name で識別 (`agentId` ではなく)。JSON status 禁止規定
  (TeamCreate tool doc 参照) を team lead プロンプトに埋め込むこと
- **親の review 疲労 (複数並列時)**: 2-4 teammate が同時に PR を揚げてくる
  と、親が review ボトルネックになる。小規模 PR のみ並列化、大規模は
  逐次に戻す。初試行は 1 teammate だけなので未観測

**失敗時の fallback**:

試行中に timeout / permission 拒否 / conflict 暴発が発生したら、その時点で
teammate を shutdown し、親セッションの worktree 逐次切替 (Baseline) に
戻す。test/commit のロールバックは各 worktree 独立なので影響局所化済。

**第 2 試行結果** (2026-04-16、2 teammate 並列、#84 docstring + #81
annotations docs、`phase-c-docs-parallel`):

- ✅ **壁時計短縮の実測**: spawn → 両 PR 作成まで実質 ~7 分 (CI 除く)。
  親が逐次 worktree 切替で同作業を行うと ~14 分相当なので半減を確認
- ❌ **lowercase でも TaskList 不可視** (初試行からの仮説を否定):
  team_name を最初から lowercase (`phase-c-docs-parallel`) で作ったが、
  team-lead からの `TaskList` も "No tasks found" を返した。case 正規化
  ミスマッチ説 (初試行で立てた仮説) は誤り。別の scoping 問題が残っており、
  **inline duplicate 方針は継続必須** (TaskList 自体を当てにしない)
- ⚠️ **worktree base branch 汚染を観測** (原因未特定、様子見):
  2 teammate の一方 (annotations-teammate) の PR #147 の初期 branch 状態に、
  他方 (docstring-teammate) の commit `6600f12` が混入していた。`gh pr diff`
  で気付き、`git rebase --empty=drop origin/main` で duplicate commit を
  drop + force-push して救済。想定原因は「先に commit を完了した teammate
  の HEAD が common dir 経由で second worktree の base に流入」だが単発
  観測のため確定不可。**mitigation**: 親は必ず `gh pr diff <N>` で scope 外
  ファイルが混入していないか目視確認し、検出したら rebase で救済する
- ⚠️ **team-lead の git 操作も sandbox に抵触**: `git switch main`
  / `git pull --ff-only` / `git branch -D` が `.git/index.lock` 書込みで
  "unable to write new index file" を出す。`dangerouslyDisableSandbox:
  true` を常用する必要がある (teammate と同条件)
- ✅ **2 teammate foreground 並列で safe**: Edit/Write permission、git
  permission (dangerouslyDisableSandbox 付き) は teammate 側で通過、timeout
  なし (doc-only scope のため負荷軽)。実装コード + pytest 反復の scope で
  同条件が通るかは別途検証

**第 2 試行からの追加教訓**:

- **PR diff は親が必ず目視する**: 各 teammate の報告を盲信せず `gh pr diff`
  で scope 外ファイルが混入していないか確認。混入があれば rebase で救済
- **TaskList は team-lead からも信用しない**: 進捗追跡は teammate からの
  SendMessage を一次情報として扱う。TaskCreate は記録用に残すが status
  更新の信頼性は低い
- **merge 順序設計**: 2 PR 並行 merge 時、先に merge した PR の commit が
  後続 PR の branch に混入している可能性に備え、先 merge → 後 rebase →
  後 merge の順を既定プロトコルにする

**第 3 試行結果** (2026-04-16、2 teammate 並列、#83 vault_reindex docstring
+ #73 folder normalize、`phase-d-small-parallel`。doc ではなく **実装コード
+ tests** の scope で初めて試行):

- ✅ **壁時計短縮 & timeout 回避**: spawn → 両 PR 作成まで ~10 分 (CI 除く)。
  実装 + pytest 反復の scope でも stream idle timeout は発生せず。SMALL〜MEDIUM
  (1 PR 3 〜 5 ファイル、Red/Green/Refactor TDD 3 commit) の負荷帯では
  foreground teammate が安定して完走できると確認
- ❌ **teammate が間違った branch で `gh pr create` する事故を観測**
  (**新規 pitfall**):
  reindex-teammate (#83 担当) が自分の push 済 branch
  `docs/issue-83-vault-reindex-docstring` ではなく、並行作業中の
  folder-teammate の branch `refactor/issue-73-folder-normalize` 上で
  `gh pr create` を実行してしまい、PR #151 が **title=#83 / contents=#73**
  という mismatch 状態で作成された。teammate 自身は「ブランチ誤切替があった
  が影響なし」と自己申告していたが、実態は PR 本体のメタデータが破綻しており
  自己検知できていなかった
- ✅ **救済は容易 (push 自体は正しかった)**: 両 teammate とも自分の作業を
  正しい branch に push 完了していたため、親が `gh pr view --json headRefName`
  で PR と branch の対応を確認し、PR #151 の title/body を #73 用に訂正 +
  `docs/issue-83-vault-reindex-docstring` から新規 PR #152 を作成して救済。
  **データロスなし**
- ✅ **衝突マトリクス設計は有効だった**: 事前に「両方 server.py を touch
  するが異なる tool docstring / 行」「indexer.py は別関数」と衝突可能性を
  解析していたため、merge 時の実衝突はゼロ。`MERGEABLE / CLEAN` で merge 成功
- ⚠️ **PR 作成に関する指示を teammate prompt に追加すべき** (次回改善):
  prompt には「ブランチ名」を明示していたが、「`gh pr create` を実行する
  直前に `git branch --show-current` で自分のブランチを確認すること」を
  明記していなかった。teammate はブランチ誤切替の事実を申告しつつも PR 作成
  時の branch 確認を忘れた

**第 3 試行からの追加教訓**:

- **PR メタデータの整合性検証を protocol に追加**: 親は必ず
  `gh pr view <N> --json headRefName,commits,files` で以下 3 点を確認:
  1. `headRefName` が teammate が spawn 時に指示した branch と一致する
  2. `commits` の prefix (Red/Green/Refactor/Docs/Test) と内容が issue と一致
  3. `files` が scope 外ファイルを含まない
  `gh pr diff` だけでは file 混入は検出できるが branch/title の mismatch は
  見逃す (第 3 試行の PR #151 は `gh pr diff` 単独では #73 の正常な diff に
  見えてしまう)
- **teammate prompt に PR 作成チェックリストを埋め込む**: 「`gh pr create`
  直前に `git branch --show-current` を実行し、期待する branch 名と一致する
  ことを確認。不一致なら `git switch <expected-branch>` してから再試行」
  という手順を明示する
- **teammate の自己申告は信用度階層で扱う**: 「〜と確認した」の report は
  事実ではなく「teammate がそう認識している」と解釈。ブランチ誤切替のような
  teammate 自身が追跡しきれない事象は、親の独立検証でしか検知できない

**第 4 試行結果** (2026-04-16〜17、4 PR 並列、Phase E1 #46+#29 統合 +
SMALL #33 / #47 / #42、`phase-e1-parallel`。**初の 4 並列・親も実装担当**):

- ⚠️ **壁時計短縮 ~20 分 vs 復旧コスト ~15 分で収支が薄い**: 4 PR 並行で
  teammate 実作業 ~10〜15 分 × 3 人で確かに壁時計は節約されたが、merge 後の
  worktree reset / 主リポ branch 掃除 / local main 復旧で ~15 分を消費。
  差し引き節約は微々たるもの
- ❌ **teammate `isolation="worktree"` が無視され主リポで作業するケース**
  (**新規 pitfall #1**): folderschema-teammate (#47) は
  `Agent(isolation="worktree", ...)` で spawn されたが、
  `git worktree list` に対応する独立 worktree が出現せず、主リポ
  (`~/Projects/individual/vault-search-mcp`) で作業していた形跡あり。
  作業終了後も主リポが `refactor/issue-47-...` branch のままで
  switch 残骸として残った。`Agent` 側の `isolation` 実装が環境依存で不安定
- ❌ **他 teammate の branch を force reset する事故** (**新規 pitfall #2**):
  folderschema-teammate が誤って `refactor/issue-42-...` (testsplit-teammate
  担当 branch) に commit、cherry-pick で自 branch に移動した後 `git branch -f
  37df8f0` で issue-42 branch を戻した。testsplit が後続 commit したため
  最終的に無被害だったが、teammate 間で branch ref 空間が共有されている
  以上、誤操作 1 回で他 teammate 全滅リスクが構造的に存在
- ❌ **親 worktree が PR merge `--delete-branch` で破壊される**
  (**新規 pitfall #3**): `gh pr merge 155 --delete-branch` 実行後、親の
  worktree HEAD が main へ自動 switch、local main が #33 の Refactor commit
  まで進み、worktree 内の `filter.py` / `indexer.py` / `tests/test_filter.py`
  が pre-E1 状態に revert されていた。`build_folder_filter_clause` が消え、
  `_folder_filter_clause` が復活する実害発生。原因仮説は teammate の主リポ
  作業 (pitfall #1) が shared `.git` 経由で親 worktree に波及 +
  `--delete-branch` の自動 switch
- ❌ **529 overloaded error で teammate が PR 作成直前に停止**
  (**新規 pitfall #4**): noascii-teammate (#33) が Red/Green/Refactor 3
  commit を push まで完了したが、`gh pr create` 直前で 529 overloaded。
  idle notification も shutdown_response も受信不能状態。push 済 branch
  から team-lead が代行 PR (#157) を作成して回収した
- ✅ **teammate 作業の成果は push さえ完了すれば回収可能**: 529 停止でも
  team-lead 代行 PR で救済できた。teammate prompt の「報告タイミング」を
  push 完了時点に前倒しする改善余地あり

**第 4 試行からの追加教訓**:

- **PR merge の `--delete-branch` が親 worktree を巻き込む**: 親が worktree
  内で作業中に `--delete-branch` 付きで merge すると、local branch ref 削除
  時に worktree HEAD が main に自動 switch + local main 汚染が発生。**回避**:
  親が worktree に居る間は `--delete-branch` を avoid、または先に
  `ExitWorktree(keep)` してから merge する
- **teammate `isolation="worktree"` は信用しない**: spawn 後に
  `git worktree list` で独立 worktree が実際に出現したか親が確認する。
  出現していなければ teammate 作業は主リポを直接触っており、他 teammate
  との branch ref 共有事故リスクが高い
- **teammate の報告タイミングは PR 作成前 (push 完了時点) に前倒しする**:
  529 / overloaded で PR 作成段階で停止しても、push 完了報告が来ていれば
  team-lead が `gh pr create` で回収できる

### 方針: 別タスク並列に agent team を使わない (2026-04-17 確立)

**4 試行を踏まえた結論**: 別 issue / 別 PR 前提の別タスクを agent team で
並列化する運用は、以下の構造的問題でワークフロー破綻しやすく、第一選択
から外す。

#### 構造的問題 (再掲)

- teammate `isolation="worktree"` が環境依存で不安定 (pitfall #1)
- branch ref 空間が teammate 間で共有され、誤操作 1 回で他 teammate
  全滅リスク (pitfall #2)
- 親が同時 worktree 作業すると、PR merge 時に worktree state が破壊される
  (pitfall #3)
- merge 後の local main 復旧 / 主リポ残骸 branch 掃除が毎回発生
- teammate parallel 数が増えるほど pitfall 発生確率が線形以上に増える

#### Agent team の適合用途 (今後もこれに限定)

- **同一 PR 内の parallel review** (`review-loop` skill) — 視点違いの
  reviewer を同時起動し 0-10 スコアで triage
- **同一 issue 内の TDD 分離** — Red delegate / Green delegate / Refactor
  計画評価。commit depend グラフが線形なら衝突しにくい
- **調査 + 実装 + テストの役割分担** (同一 issue 内)
- **機械的 edit の broadcast** (docstring 多数ファイル、annotation 付与等) —
  衝突マトリクスが低いブロードキャスト型

特徴: 親-子の明確な委譲、共通の成果物 (1 PR)、進捗集約しやすい。

#### Agent team の非適合用途 (今後使わない)

- 別 issue / 別 PR 前提の並列化
- 広範囲 edit の分散 (>500 行、>1 ファイル横断)
- teammate 同士の依存が見えにくい複合タスク

#### 推奨代案: 別セッション + 各セッション内で worktree 切替

別タスク並列を行うなら、agent team の外側で **別 Claude Code セッションを
人間側で複数起動**し、各セッションで親自身が `EnterWorktree` で独自
worktree に入る方式を推奨:

- セッション境界が teammate 間の強い isolation (OS プロセス + サンドボックス)
- サンドボックスの cwd が worktree に固定されるので、誤って主リポを触る
  事故が構造的に防がれる
- 各セッションで PR 作成 / merge のタイミングが独立、干渉ゼロ
- merge 後復旧は各セッション内で完結

**制約**: 人間側の切替コスト (タブ / ウィンドウ / コンテキスト管理) が増える。
ただし agent team の merge 後復旧コストを考えれば切替コストのほうが安い。

### Teammate prompt 標準テンプレート (3 試行の蓄積を集約)

2-4 teammate の並列 spawn 時、teammate prompt に以下のセクションを**必ず**
埋め込む。3 回の試行で判明した pitfall を全て回避するための最低要件。

より詳細な boilerplate は `.claude/skills/teammate-spawn/SKILL.md` を参照。

#### 必須セクション

1. **Scope** — 対象 issue 番号、完了条件、禁止事項 (scope 外ファイル)
2. **環境**:
   - `dangerouslyDisableSandbox: true` が必要な場面 (git commit/push、
     `rm .venv && uv sync --all-extras`)
   - `.venv/bin/pytest` / `.venv/bin/ruff` 直接呼出 (`uv run` は sandbox で死ぬ)
3. **commit 規約**: Red/Green/Refactor 独立 commit、prefix 規約
4. **PR 作成 checklist** (第 3 試行の新規 pitfall 対策):
   - `gh pr create` **直前に** `git branch --show-current` を実行
   - 期待する branch 名と一致しない場合は `git switch <expected>` してから再試行
   - PR 作成後、`gh pr view <N> --json headRefName,commits,files` で
     自分の PR のメタデータを目視確認
5. **報告**: 完了時に team-lead へ `SendMessage` で PR 番号と CI status を送る
6. **Shutdown プロトコル**: `shutdown_request` を受けたら
   `shutdown_response({approve: true})` で自発終了
7. **禁止事項 (衝突対策)**: 他 teammate が触るファイルを列挙し「絶対に触らない」
   を明示

#### 親側の protocol

- **事前**: 衝突マトリクス (どの teammate が何ファイルを触るか) を必ず作成
- **teammate spawn**: `isolation: "worktree"` + `model: "sonnet"` +
  `subagent_type: "general-purpose"` + `team_name` + `name` を全て設定
- **PR 受領時**: `gh pr view <N> --json headRefName,commits,files` で
  3 点検証 (branch / commits / files)。teammate の自己申告だけで merge しない
- **merge 順**: 先 merge → 後 rebase (必要なら `git rebase --empty=drop
  origin/main`) → 後 merge
- **shutdown 順**: teammate 全員 idle 確認 → `shutdown_request` →
  `teammate_terminated` 通知 → `TeamDelete`

## review-loop のモデル選定 (実験中: Sonnet 全視点 vs Sonnet+Opus)

**2026-04-20 時点で実験中**。現状 2 方針を並走させ、将来サンプルが揃った段階で
片方に絞る判定を行う。

### 方針 A: Sonnet+Opus 混成 (従来、`.claude/skills/review-loop/SKILL.md` 規定値)

Architecture 視点 (D) のみ Opus、A/B/C は Sonnet。層越え・結合度の抽象判断で
Opus の推論力が効く前提。

### 方針 B: Sonnet 全視点 (実験中、低コスト仮説)

A/B/C/D 全て Sonnet。PR #204 / #209 / #213 の 3 連続で「3 Round で CONVERGED」が
再現しており、方針 A 比でコスト削減見込み (R1 の 4 並列で Opus 1→0)。

### 判定基準

現時点では **同一 PR に Sonnet 単独 と Sonnet+Opus 混成 の両方を並行実行し、
findings の重なり・独立性を観察する実験を数回は積む** 必要がある。以下のような
メトリクスを記録する:

- 方針 B で見逃された方針 A 固有の finding (スコア ≥5) の数
- 方針 A で余計に出た低価値 finding (追認や重複) の数
- Round 2 以降の収束速度差

定量的な差が見えなければ、コスト面から方針 B (Sonnet 全視点) を default 化する。
逆に Opus D が毎回ユニークな ≥6 を拾うなら方針 A を維持する。

**現状の暫定運用**: 既定は方針 A (SKILL.md 通り)。明示的に「Sonnet で全視点」と
user が指示した場合のみ方針 B を採用し、事例を蓄積する。事例ログは本節の下に
「## review-loop 実験ログ」として追記していく (未着手)。

### 他モデル構成の確立済み運用 (変更対象外)

`## サブエージェントへの delegate` § 「モデル指定指針」に記載の Refactor 計画
評価 (Opus) 等は本実験の対象外。実装は Sonnet、設計評価は Opus の 2 段構成は
複数 PR で効果確認済み (PR #96 での NO-GO 実績等)。

## commit メッセージの prefix 規約

- `Red:` — 失敗テスト追加
- `Green:` — 最小実装でテスト通過
- `Refactor:` — 妥協解消 / 整理 (機能変更なし)
- `Test:` — Green/Refactor 以外のテスト追加・強化
- `Docs:` — README / CLAUDE.md / rules / skill 更新
- `Simplify:` — `/simplify` レビューによる改善
- `Fix:` — バグ修正 (TDD 外で小修正する場合のみ。通常は Red/Green 形式推奨)
