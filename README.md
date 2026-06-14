# piscare-subsidy-watch

福岡県久留米市の訪問看護事業者（ピスケア）向けに、補助金・助成金情報を Slack に集約する仕組みの設定ログ・実装リポジトリ。

## 現在の構成（v0・2026-06-12 稼働開始）

- Slack: piscare ワークスペースの鍵付きチャンネル `#補助金情報`
- 購読フィード 5 本（`/feed list` で確認可能、最終設計は [Issue #5](../../issues/5) 参照）
  - Google アラート 5 本: 久留米市 / 福岡県医療介護 / 厚労省看護介護助成 / 中小企業向け国補助金 / 車両（CEV補助金等）
- 週次サマリ: GitHub Actions cron が毎週月曜 9:00 JST に [data/subsidy-master.yaml](data/subsidy-master.yaml) を読み Slack に投稿（[Issue #4](../../issues/4)）
- 語彙定義は [CONTEXT.md](CONTEXT.md) 参照

設定値の正確な記録（クエリ・フィード URL・変更履歴）は [Issues](../../issues?q=is%3Aissue) に残す。
フィードに流れにくい定番制度は [data/subsidy-master.yaml](data/subsidy-master.yaml) で管理する。

## 段階導入計画

| 段階 | 内容 | 状態 |
|---|---|---|
| v0 | Google アラート + Slack `/feed`（ゼロコード） | 稼働中 |
| v1 | 定番マスタ YAML + 週次サマリリマインダー（Issue #4）/ 狭域クエリ収集スクリプト（Issue #3） | 着手中 |
| v2 | LLM（Claude Haiku）によるノイズフィルタ | 構想 |

## 背景

補助金の供給源は 1 つではなく 4 層に分かれており（経産省系 jGrants / 厚労省雇用助成金 / 福岡県基金事業 / 久留米市）、単一ソースでの網羅は不可能と検証済み。フィードに流れない定番制度（業務改善助成金等）はマスタデータとして別管理する方針。

## 運用メモ

- Slack Incoming Webhook URL 等の秘密情報はこのリポジトリに書かない（GitHub Actions Secrets で管理する）
- フィードの追加・削除・クエリ変更は都度 Issue に記録する
