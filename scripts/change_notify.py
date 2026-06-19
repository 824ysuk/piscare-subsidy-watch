#!/usr/bin/env python3
"""定番補助金マスタの変化を PR マージ時に Slack に投稿する。

data/subsidy-master.yaml を含む PR がマージされたとき、verification_status・
status・next_event / next_event_kind の変化を制度単位で列挙して投稿する。
変化がなければ何も投稿せずに終了する（exit 0）。

ローカル検証:
  python scripts/change_notify.py --before-file /tmp/before.yaml --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

from _schema import STATUS_LABELS, VERIFICATION_LABELS, validate_items

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_PATH = REPO_ROOT / "data" / "subsidy-master.yaml"
MASTER_URL = "https://github.com/824ysuk/piscare-subsidy-watch/blob/main/data/subsidy-master.yaml"
REPO_URL = "https://github.com/824ysuk/piscare-subsidy-watch"

_FIELD_LABELS: dict[str, str] = {
    "verification_status": "確認状況",
    "status": "申請状況",
    "next_event": "次回日程",
    "next_event_kind": "イベント種別",
}


def _human_value(field: str, value: Any) -> str:
    """内部値を Slack 表示用の日本語に変換する。"""
    if value is None:
        return "（未設定）"
    if field == "verification_status":
        return VERIFICATION_LABELS.get(str(value), str(value))
    if field == "status":
        return STATUS_LABELS.get(str(value), str(value))
    # date オブジェクトは YYYY年M月D日 形式に
    from datetime import date as _date
    if isinstance(value, _date):
        return f"{value.year}年{value.month}月{value.day}日"
    return str(value)


def load_yaml_str(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"master root must be a mapping, got {type(data).__name__}")
    return data


def load_master(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return load_yaml_str(f.read())


def git_show_master(sha: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{sha}:data/subsidy-master.yaml"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git show failed: {result.stderr.strip()}")
    return result.stdout


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def detect_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    """verification_status・status・next_event / next_event_kind の変化を検出する。

    返す各要素:
      item: 変化後（削除時は変化前）の制度 dict
      kind: "added" | "removed" | "closed_confirmed" | "modified"
      diffs: 変化の説明文リスト
    """
    before_map: dict[str, dict[str, Any]] = {
        item["id"]: item for item in (before.get("items") or [])
    }
    after_map: dict[str, dict[str, Any]] = {
        item["id"]: item for item in (after.get("items") or [])
    }

    changes: list[dict[str, Any]] = []

    for item_id, item in before_map.items():
        if item_id not in after_map:
            changes.append({"item": item, "kind": "removed", "diffs": ["マスタから削除"]})

    for item_id, item in after_map.items():
        if item_id not in before_map:
            changes.append({"item": item, "kind": "added", "diffs": ["新規追加"]})
            continue

        before_item = before_map[item_id]
        diffs: list[str] = []

        bv = before_item.get("verification_status")
        av = item.get("verification_status")
        if bv != av:
            label = _FIELD_LABELS["verification_status"]
            diffs.append(f"{label}: {_human_value('verification_status', bv)} → {_human_value('verification_status', av)}")

        bs = before_item.get("status")
        as_ = item.get("status")
        if bs != as_:
            label = _FIELD_LABELS["status"]
            diffs.append(f"{label}: {_human_value('status', bs)} → {_human_value('status', as_)}")

        b_sched = before_item.get("schedule") or {}
        a_sched = item.get("schedule") or {}

        if _str(b_sched.get("next_event")) != _str(a_sched.get("next_event")):
            label = _FIELD_LABELS["next_event"]
            diffs.append(f"{label}: {_human_value('next_event', b_sched.get('next_event'))} → {_human_value('next_event', a_sched.get('next_event'))}")

        if b_sched.get("next_event_kind") != a_sched.get("next_event_kind"):
            label = _FIELD_LABELS["next_event_kind"]
            diffs.append(f"{label}: {_human_value('next_event_kind', b_sched.get('next_event_kind'))} → {_human_value('next_event_kind', a_sched.get('next_event_kind'))}")

        if not diffs:
            continue

        # status:closed_or_unannounced かつ verification_status:verified = 事業終了確認
        is_closed_confirmed = (
            item.get("status") == "closed_or_unannounced"
            and item.get("verification_status") == "verified"
        )
        kind = "closed_confirmed" if is_closed_confirmed else "modified"
        changes.append({"item": item, "kind": kind, "diffs": diffs})

    return changes


def _item_line(item: dict[str, Any], icon: str) -> str:
    urls = item.get("source_urls") or []
    url_part = f" (<{urls[0]}|一次情報>)" if urls else ""
    return f"- {icon} {item['name']}{url_part}"


def build_message(
    changes: list[dict[str, Any]], pr_number: int | None = None
) -> str:
    if pr_number:
        pr_url = f"{REPO_URL}/pull/{pr_number}"
        header = f":bell: 定番マスタ更新 (<{pr_url}|PR #{pr_number}>)"
    else:
        header = ":bell: 定番マスタ更新"

    lines: list[str] = [header, ""]

    closed = [c for c in changes if c["kind"] == "closed_confirmed"]
    added = [c for c in changes if c["kind"] == "added"]
    removed = [c for c in changes if c["kind"] == "removed"]
    modified = [c for c in changes if c["kind"] == "modified"]

    if closed:
        lines.append("*▼ 事業終了確認*")
        for c in closed:
            lines.append(_item_line(c["item"], ":no_entry:"))
            for d in c["diffs"]:
                lines.append(f"  • {d}")
        lines.append("")

    if added:
        lines.append("*▼ 新規追加*")
        for c in added:
            lines.append(_item_line(c["item"], ":new:"))
        lines.append("")

    if modified:
        lines.append("*▼ 変化あり*")
        for c in modified:
            lines.append(_item_line(c["item"], ":pencil2:"))
            for d in c["diffs"]:
                lines.append(f"  • {d}")
        lines.append("")

    if removed:
        lines.append("*▼ 削除*")
        for c in removed:
            lines.append(_item_line(c["item"], ":wastebasket:"))
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
    parser = argparse.ArgumentParser(description="マスタ変化を Slack に投稿")
    parser.add_argument("--dry-run", action="store_true", help="Slack に投稿せず標準出力に出す")
    parser.add_argument("--before-file", help="変更前 YAML のパス（ローカル検証用）")
    parser.add_argument("--after-file", help="変更後 YAML のパス（ローカル検証用、省略時は data/subsidy-master.yaml）")
    args = parser.parse_args()

    if args.before_file:
        before = load_master(Path(args.before_file))
    else:
        base_sha = os.environ.get("BASE_SHA")
        if not base_sha:
            print("ERROR: BASE_SHA is not set", file=sys.stderr)
            return 1
        before = load_yaml_str(git_show_master(base_sha))

    after = load_master(Path(args.after_file)) if args.after_file else load_master(MASTER_PATH)
    validate_items(after.get("items") or [])

    changes = detect_changes(before, after)
    if not changes:
        print("変化なし。投稿をスキップします。", file=sys.stderr)
        return 0

    pr_number_str = os.environ.get("PR_NUMBER")
    pr_number = int(pr_number_str) if pr_number_str else None
    text = build_message(changes, pr_number)

    if args.dry_run:
        print(text)
        return 0

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return 1

    post_to_slack(webhook_url, text)
    print(f"Posted change summary ({len(changes)} changes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
