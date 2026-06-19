#!/usr/bin/env python3
"""定番補助金マスタの週次サマリを Slack に投稿する。

CONTEXT.md の schema 定義に従い data/subsidy-master.yaml を読み、
target_eligible:false と status:closed_or_unannounced / not_target を除外したうえで、
直近 6 ヶ月の締切 / 受付中 / 告知監視期 / 通年制度 の 4 セクションに分類して投稿する。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

from _schema import validate_items

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_PATH = REPO_ROOT / "data" / "subsidy-master.yaml"

# ピスケア運用上、半年先までの締切を把握できれば事業計画に組み込める想定で 180 日固定。
LOOKAHEAD_DAYS = 180

# 表示から除外するステータス。closed_or_unannounced は終了済み、not_target は target_eligible:false と pair。
EXCLUDED_STATUSES = {"closed_or_unannounced", "not_target"}

# 告知監視期セクションに含めるステータス。CONTEXT.md の status 6 値と整合。
MONITORING_STATUSES = {"monitoring", "preparing"}

MASTER_URL = "https://github.com/824ysuk/piscare-subsidy-watch/blob/main/data/subsidy-master.yaml"


def load_master(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"master root must be a mapping, got {type(data).__name__}")
    return data


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def is_displayed(item: dict[str, Any]) -> bool:
    if item.get("target_eligible") is False:
        return False
    if item.get("status") in EXCLUDED_STATUSES:
        return False
    return True


def conditional_marker(item: dict[str, Any]) -> str:
    return " ⚠️" if item.get("target_eligible") == "conditional" else ""


def classify(items: list[dict[str, Any]], today: date) -> dict[str, list[dict[str, Any]]]:
    """schedule.type と status をもとに 4 セクションに振り分ける。

    返り値の key:
      - upcoming: fixed_date で next_event が今日 〜 +180 日以内
      - open_since: 受付開始済みで継続中 (CEV 等)
      - monitoring: 告知監視期 / 準備中
      - ongoing: 通年制度
    """
    upcoming: list[dict[str, Any]] = []
    open_since: list[dict[str, Any]] = []
    monitoring: list[dict[str, Any]] = []
    ongoing: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []

    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)

    for item in items:
        if not is_displayed(item):
            continue
        schedule = item.get("schedule") or {}
        stype = schedule.get("type")
        status = item.get("status")
        next_event = parse_date(schedule.get("next_event"))

        if stype == "fixed_date" and next_event and today <= next_event <= cutoff:
            upcoming.append(item)
        elif stype == "open_since" and status == "open":
            open_since.append(item)
        elif stype == "ongoing":
            ongoing.append(item)
        elif status in MONITORING_STATUSES:
            monitoring.append(item)
        else:
            unclassified.append(item)

    if unclassified:
        ids = [item.get("id", "<no id>") for item in unclassified]
        raise ValueError(
            f"週次サマリに分類できない制度があります: {ids}\n"
            "status / schedule.type の組み合わせを確認してください。"
        )

    upcoming.sort(key=lambda x: parse_date(x["schedule"]["next_event"]) or date.max)

    return {
        "upcoming": upcoming,
        "open_since": open_since,
        "monitoring": monitoring,
        "ongoing": ongoing,
    }


def _format_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value is not None else ""


def build_message(
    classified: dict[str, list[dict[str, Any]]],
    today: date,
    verified_at: Any,
) -> str:
    lines: list[str] = [f":calendar: ピスケア 補助金マスタ 週次サマリ ({today.isoformat()})"]
    verified_str = _format_date(verified_at)
    if verified_str:
        lines.append(f"（マスタ最終検証日: {verified_str}）")
    lines.append("")

    upcoming = classified["upcoming"]
    if upcoming:
        lines.append("*▼ 直近 6 ヶ月の締切*")
        for item in upcoming:
            schedule = item["schedule"]
            d_str = _format_date(schedule.get("next_event"))
            kind = schedule.get("next_event_kind", "")
            kind_suffix = f"（{kind}）" if kind else ""
            lines.append(f"- {d_str}: {item['name']}{kind_suffix}{conditional_marker(item)}")
        lines.append("")

    open_since = classified["open_since"]
    if open_since:
        lines.append("*▼ 受付中*")
        for item in open_since:
            schedule = item["schedule"]
            d_str = _format_date(schedule.get("next_event"))
            kind = schedule.get("next_event_kind", "")
            if kind and d_str:
                detail = f"（{kind} {d_str}〜）"
            elif d_str:
                detail = f"（{d_str}〜）"
            elif kind:
                detail = f"（{kind}）"
            else:
                detail = ""
            lines.append(f"- {item['name']}{detail}{conditional_marker(item)}")
        lines.append("")

    monitoring = classified["monitoring"]
    if monitoring:
        lines.append("*▼ 告知監視期*")
        for item in monitoring:
            schedule = item["schedule"]
            window = schedule.get("monitoring_window")
            kind = schedule.get("next_event_kind", "")
            note_parts: list[str] = []
            if window:
                note_parts.append(str(window))
            if kind:
                note_parts.append(str(kind))
            detail = f"（{' / '.join(note_parts)}）" if note_parts else ""
            lines.append(f"- {item['name']}{detail}{conditional_marker(item)}")
        lines.append("")

    ongoing = classified["ongoing"]
    if ongoing:
        lines.append("*▼ 通年制度*")
        labels = [item["name"] + conditional_marker(item) for item in ongoing]
        lines.append(" / ".join(labels))
        lines.append("")

    lines.append(f"詳細: <{MASTER_URL}|data/subsidy-master.yaml>")
    return "\n".join(lines)


def post_to_slack(webhook_url: str, text: str) -> None:
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Slack POST failed: HTTP {resp.status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="週次補助金サマリを Slack に投稿")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack には投稿せず標準出力に出す",
    )
    parser.add_argument(
        "--today",
        help="今日の日付を上書き (YYYY-MM-DD、ローカル検証用)",
    )
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    master = load_master(MASTER_PATH)
    items = master.get("items") or []
    if not isinstance(items, list):
        raise ValueError(f"master.items must be a list, got {type(items).__name__}")
    validate_items(items)

    classified = classify(items, today)
    text = build_message(classified, today, master.get("verified_at"))

    if args.dry_run:
        print(text)
        return 0

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return 1

    post_to_slack(webhook_url, text)
    print(f"Posted weekly summary at {today}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
