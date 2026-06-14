# piscare-subsidy-watch CONTEXT

ユビキタス言語集。コードや YAML を読むだけでは意図が伝わらない用語の定義をここに集約する。
正本は `data/subsidy-master.yaml`（PR #7 でマージ済み）。本ドキュメントは同 YAML の schema を運用視点から言語化する。

## ドメイン語彙

### 補助金マスタ (subsidy master)

`data/subsidy-master.yaml` に集約する「定番制度」のデータベース。フィード型収集（Google アラート / J-Net21 RSS）では取りこぼす制度を補完する目的で、年次スケジュールがほぼ固定の制度のみを管理する。

### 定番制度 (staple subsidy)

以下のいずれかの理由で「フィードに流れない or 流れても気づきにくい」制度。Issue #4 で 11 制度を初期登録。

- 厚労省雇用助成金: jGrants 非掲載
- 福岡県基金事業: 県庁ページのみで RSS なし
- 久留米市の補助金: 市サイトのみで RSS なし
- 一部経産省系: 公募開始通知が告知サイトに載らず本サイトのみ更新

## YAML schema (`data/subsidy-master.yaml`)

```yaml
schema_version: 1
verified_at: <YYYY-MM-DD>      # マスタ全体の最終検証日
items:
  - id: <kebab-case unique>
    name: <制度名>
    owner: <所管>
    category: <分類>           # employment / digitalization / vehicle / 等
    target_eligible: true | false | conditional
    target_reason: <判定理由・条件付き判定の補足>
    schedule:
      type: fixed_date | monitoring | monitoring_window | open_since | ongoing
      next_event: <YYYY-MM-DD> | null
      next_event_kind: <受付開始 / 申請締切 / 公募開始 / 告知監視 等>
      monitoring_window: <監視期間>  # type が monitoring / monitoring_window 系のとき
    status: upcoming | open | monitoring | closed_or_unannounced | not_target | preparing
    verification_status: verified | needs_recheck | partial
    source_urls:
      - <一次情報源 URL>
    notes:
      - <補足 1>
      - <補足 2>
```

### `schedule.type` の 5 値

各制度の開催形態を表す enum 相当の値。

| 値 | 意味 | 11 制度内の典型例 |
|---|---|---|
| `fixed_date` | 次回開催の特定日（受付開始 / 締切）が確定 | 業務改善助成金 R8（2026-09-01 受付開始）/ デジタル化・AI導入（2026-06-15 締切） |
| `monitoring` | 監視中（告知待ち or 後継待ち、明確な期間なし） | 福岡県勤務環境改善促進費（後継未発表）/ 久留米市WLB / 持続化補助金 第20回 / 日本財団 |
| `monitoring_window` | 監視期間が明示されている（例年 6-7 月告知 等） | 福岡県介護DX 補助金 R8 |
| `open_since` | 受付開始済みで継続中（締切日も提示済み） | CEV 補助金（2026-03-31 申請受付開始） |
| `ongoing` | 通年（常時申請可） | キャリアアップ / 人材開発支援 / 両立支援等 |

### `target_eligible` の 3 値

ピスケア（**株式会社**）にとって対象となる制度かどうかの判定。

| 値 | 意味 | 通知での扱い |
|---|---|---|
| `true` | 対象 | 通常通り表示 |
| `conditional` | 条件付き対象（要件確認が必要） | 表示。`target_reason` の条件を併記 |
| `false` | 対象外 | 通知から除外（YAML には残す） |

**`false` の制度も YAML から削除しない**。判定履歴を運用知見として永続化することで、同じ制度を Web で再発見したときの再判定コストを下げる。

### `status` の値

申請状況の現在ステート。`schedule.type` がスケジュール構造を表すのに対し、`status` は時間軸上の現在位置を表す。

| 値 | 意味 |
|---|---|
| `upcoming` | 受付開始日が将来 |
| `open` | 受付中 |
| `monitoring` | 告知待ち / 監視中 |
| `closed_or_unannounced` | 過去回終了・後継未発表 |
| `not_target` | ピスケア対象外（`target_eligible: false` と通常 pair） |
| `preparing` | 公式が次回準備中（持続化補助金 第20回 等） |

### `verification_status`

一次情報による検証状態。半期再検証フローの入力。

| 値 | 意味 |
|---|---|
| `verified` | 公式ページで対象規定・スケジュール両方を確認済み |
| `partial` | 一部のみ確認（URL は取れたが詳細締切日は未確認 等） |
| `needs_recheck` | 直近で要再確認（古いデータ・状況変化が予想される） |

### ピスケアの法人形態

**株式会社**。`target_eligible` 判定の入力。

- 日本財団 福祉車両助成 = 株式会社対象外 → `target_eligible: false`
- 持続化補助金 第20回 = 医療法人除外（株式会社は対象だが従業員数要件あり）→ `target_eligible: conditional`
- 久留米市WLB = くるみん等国認定が前提 → `target_eligible: conditional`
- CEV 補助金 = 白ナンバー自家用社用車のみ対象 → `target_eligible: conditional`

## 週次サマリ (weekly summary)

毎週月曜 9:00 JST に `#補助金情報` 鍵付きチャンネルに投稿する Slack メッセージ。`data/subsidy-master.yaml` を読み、以下の方針で組み立てる:

1. **除外**: `target_eligible: false` または `status: closed_or_unannounced` の制度は表示しない
2. **直近 6 ヶ月の締切**: `schedule.next_event` が今日から 180 日以内の制度を日付順に列挙（`schedule.type: fixed_date` / `open_since` が中心）
3. **監視期**: `status: monitoring` または `preparing` の制度をセクション化
4. **通年制度**: `schedule.type: ongoing` の制度を末尾に名前のみ圧縮列挙
5. **条件付き対象**: `target_eligible: conditional` には ⚠️ マークまたは「（要件確認）」を併記

Slack 1 画面で全体把握できる量に収める。

## 段階導入計画と本ドキュメントの位置づけ

| 段階 | 内容 | 状態 |
|---|---|---|
| v0 | Google アラート 5 本 + Slack `/feed`（ゼロコード） | 稼働中 |
| v1 | 補助金マスタ YAML（Issue #4・データ層 PR #7 マージ済み）+ 週次サマリリマインダー / 狭域クエリ収集スクリプト（Issue #3） | 着手中 |
| v2 | LLM（Claude Haiku）によるノイズフィルタ | 構想 |

本 CONTEXT.md は v1 着手と同時に作成。v2 で語彙が変わる場合は再編集する。
