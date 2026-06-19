"""subsidy-master.yaml の schema 定義と表示用ラベル.

ALLOWED_* は「現行 YAML で新規挿入を許す値」(validate 用).
*_LABELS は「Slack 表示用」(過去 YAML との diff 表示で過去値も含む後方互換).

CONTEXT.md の status / verification_status 表と本ファイルの ALLOWED_* は人手同期.
値追加時は両方更新する.
"""
from __future__ import annotations

from typing import Any

# CONTEXT.md「status の値」セクション準拠（6 値）
ALLOWED_STATUSES: frozenset[str] = frozenset({
    "upcoming",
    "open",
    "monitoring",
    "closed_or_unannounced",
    "not_target",
    "preparing",
})

# CONTEXT.md「verification_status」セクション準拠（3 値）
ALLOWED_VERIFICATION_STATUSES: frozenset[str] = frozenset({
    "verified",
    "partial",
    "needs_recheck",
})

# Slack 表示用ラベル（change_notify.py 用）
# 過去 YAML との diff 表示で過去値が登場するため、過去値も含めて後方互換維持。
# 過去値（廃止 / 新規挿入禁止）はコメントで明示する。
STATUS_LABELS: dict[str, str] = {
    "open": "申請受付中",
    "upcoming": "公募予定",
    "monitoring": "告知監視中",
    "preparing": "準備中",
    "closed_or_unannounced": "終了/未発表",
    "not_target": "対象外",
    # 過去値: 受付中か監視中か判別できない暫定状態として使用されていた値。
    # 現行 YAML では未使用。過去 commit との diff 表示で登場するため後方互換維持。
    # 新規挿入は ALLOWED_STATUSES で禁止（git log -S open_or_monitoring で履歴追跡可能）。
    "open_or_monitoring": "受付中/監視中",
}

VERIFICATION_LABELS: dict[str, str] = {
    "verified": "確認済み",
    "needs_recheck": "要再確認",
    "partial": "一部確認",
}

# 不変条件: ALLOWED は LABELS の部分集合（label 不在の正規値があってはならない）
assert ALLOWED_STATUSES <= STATUS_LABELS.keys(), (
    f"ALLOWED_STATUSES に対応する STATUS_LABELS エントリがありません: "
    f"{ALLOWED_STATUSES - STATUS_LABELS.keys()}"
)
assert ALLOWED_VERIFICATION_STATUSES <= VERIFICATION_LABELS.keys(), (
    f"ALLOWED_VERIFICATION_STATUSES に対応する VERIFICATION_LABELS エントリがありません: "
    f"{ALLOWED_VERIFICATION_STATUSES - VERIFICATION_LABELS.keys()}"
)


def validate_items(items: list[dict[str, Any]]) -> None:
    """items[] の status / verification_status を検証。違反は ValueError。

    現行マスタにのみ適用する（過去 YAML には適用しない。過去値が混入しうるため）。
    複数違反がある場合は全件まとめて raise する。
    """
    errors: list[str] = []
    for item in items:
        item_id = item.get("id", "<no id>")
        status = item.get("status")
        if status is not None and status not in ALLOWED_STATUSES:
            errors.append(
                f"{item_id}: status={status!r} は許容値外です "
                f"({sorted(ALLOWED_STATUSES)})"
            )
        vstatus = item.get("verification_status")
        if vstatus is not None and vstatus not in ALLOWED_VERIFICATION_STATUSES:
            errors.append(
                f"{item_id}: verification_status={vstatus!r} は許容値外です "
                f"({sorted(ALLOWED_VERIFICATION_STATUSES)})"
            )
    if errors:
        raise ValueError(
            "subsidy-master.yaml schema violations:\n  - " + "\n  - ".join(errors)
        )
