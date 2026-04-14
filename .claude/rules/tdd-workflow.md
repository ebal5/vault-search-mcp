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

## 並行作業時の調停

複数サブエージェントが src/ / tests/ を同時に触る場合:

- 親 Claude が git stash / rebase を管理 (サブエージェントには委ねない)
- 各エージェントに「他エージェントが同時進行している可能性」を共有
- `git pull --rebase` 相当の自動調停は禁止 (意図しないマージは親の責任)
- 同一ファイルを 2 エージェントが書く予定があれば逐次化、並列しない

## commit メッセージの prefix 規約

- `Red:` — 失敗テスト追加
- `Green:` — 最小実装でテスト通過
- `Refactor:` — 妥協解消 / 整理 (機能変更なし)
- `Test:` — Green/Refactor 以外のテスト追加・強化
- `Docs:` — README / CLAUDE.md / rules / skill 更新
- `Simplify:` — `/simplify` レビューによる改善
- `Fix:` — バグ修正 (TDD 外で小修正する場合のみ。通常は Red/Green 形式推奨)
