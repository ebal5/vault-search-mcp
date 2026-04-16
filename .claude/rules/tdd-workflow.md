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

## 複数 issue の並列運用パターン

Phase A/B のように「互いにほぼ独立した 2-4 issue を短時間でまとめて片付けたい」
場合の運用指針。試行結果を蓄積しつつ更新していく (2026-04-16 時点)。

### Baseline: 親セッション worktree 逐次切替 (実績あり)

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

### 試行錯誤中: TeamCreate ベースの parallel PDCA

`TeamCreate` で team を作り、親 (team lead) が issue ごとに worktree 付き
teammate を spawn して並列実行する案。2026-04-16 時点で未試行。次回セッションで
SMALL × 2 issue 程度で試す想定。

**想定フロー**:

1. 親が `TeamCreate(team_name="phase-X-parallel")` で team を開く
2. 親が各 issue を `TaskCreate` で team 共通タスクリストに登録 (completion
   条件・禁止事項・lint/test の完了チェック項目を含める)
3. 各 teammate を `Agent(subagent_type="general-purpose", team_name=...,
   isolation="worktree")` で spawn。`general-purpose` は Edit 可能、
   `isolation: "worktree"` で独立 git worktree が切られる
4. teammate は Red → Green → Refactor → PR 作成まで完結し `TaskUpdate` で
   完了報告
5. 親は teammate からの完了メッセージ (自動配信) を受けて review / merge 判断
6. 全 issue 完了後 `TeamDelete` で cleanup

**期待される効果**:

- 壁時計時間: 最も遅い teammate 1 件分で済む (真の並列)
- 親コンテキスト節約: teammate が Red/Green/Refactor 往復を吸収
- 設計判断の分担: 親は triage / integration、teammate は実装

**懸念点 / 事前検証事項**:

- **`isolation: "worktree"` で Edit/Write が通るか**: foreground / background
  の区別、permission 継承が実測未確認。最初の試行では単純な #120 相当
  (exceptions.py のみ触る) から始めて確認
- **timeout リスク**: 広範囲 edit では ~90 分 stream idle timeout
  (§既知の落とし穴) が再発する可能性。teammate が timeout した場合、
  partial edit を親が引き継げる設計にしておく (task description に
  「中断した場合の recovery 指針」を明記)
- **同ファイル衝突**: 各 teammate が独立 worktree を持つなら conflict は
  merge 時に発生。事前に衝突マトリクスを組み、衝突可能性ある issue は
  同一 team で並走させない (Phase を切る)
- **メッセージの取り回し**: teammate は plain text で報告する。親は
  teammate を name で識別 (`agentId` ではなく)。JSON status 禁止規定
  (TeamCreate tool doc 参照) を team lead プロンプトに埋め込むこと
- **親の review 疲労**: 2-4 teammate が同時に PR を揚げてくると、親が
  review ボトルネックになる。小規模 PR のみ並列化、大規模は逐次に戻す

**失敗時の fallback**:

試行中に timeout / permission 拒否 / conflict 暴発が発生したら、その時点で
teammate を shutdown し、親セッションの worktree 逐次切替 (Baseline) に
戻す。test/commit のロールバックは各 worktree 独立なので影響局所化済。

## commit メッセージの prefix 規約

- `Red:` — 失敗テスト追加
- `Green:` — 最小実装でテスト通過
- `Refactor:` — 妥協解消 / 整理 (機能変更なし)
- `Test:` — Green/Refactor 以外のテスト追加・強化
- `Docs:` — README / CLAUDE.md / rules / skill 更新
- `Simplify:` — `/simplify` レビューによる改善
- `Fix:` — バグ修正 (TDD 外で小修正する場合のみ。通常は Red/Green 形式推奨)
