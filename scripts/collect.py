#!/usr/bin/env python3
"""J-Net21 と jGrants API から補助金情報を収集し Slack に投稿する。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state" / "seen_urls.json"

JGRANTS_ENDPOINT = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"
# id は Salesforce 形式文字列。詳細 URL は推定形式のため運用時にリンク切れを確認すること。
JGRANTS_DETAIL_BASE = "https://jgrants-portal.go.jp/subsidy/"

JNET21_ENDPOINT = "https://j-net21.smrj.go.jp/snavi2/results.php"
JNET21_ARTICLE_BASE = "https://j-net21.smrj.go.jp/snavi2/"

# ピスケア（久留米市・訪問看護）向け狭域クエリ
JNET21_QUERIES: list[str] = ["久留米", "福岡 訪問看護", "福岡 介護", "福岡 看護"]
JGRANTS_QUERIES: list[str] = ["福岡", "介護", "訪問看護"]

# J-Net21 地域フィルタ: タイトルに含まれるべき文字列
REGION_INCLUDE: tuple[str, ...] = ("久留米", "福岡", "全国")
# 東久留米市（東京都）は久留米クエリの誤ヒット率が高い（実測で6件中4件）ため除外
REGION_EXCLUDE: tuple[str, ...] = ("東久留米",)

# jGrants 地域フィルタ: target_area_search に含まれるべき文字列
JGRANTS_REGION_INCLUDE: tuple[str, ...] = ("福岡", "全国")

_USER_AGENT = "piscare-subsidy-watch/1.0"


def load_seen_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("seen_urls") or [])
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_urls(path: Path, urls: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"seen_urls": sorted(urls)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get(url: str, timeout: int = 15) -> bytes | None:
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (URLError, HTTPError) as exc:
        print(f"WARNING: GET {url} failed: {exc}", file=sys.stderr)
        return None


def fetch_jnet21(query: str) -> list[dict[str, str]]:
    url = f"{JNET21_ENDPOINT}?{urlencode({'freeWord': query, 'period': '0'})}"
    raw = _get(url)
    if raw is None:
        return []
    html = raw.decode("utf-8", errors="replace")
    pattern = re.compile(r'<a[^>]+href="(articles/\d+)"[^>]*>(.*?)</a>', re.DOTALL)
    items: list[dict[str, str]] = []
    for m in pattern.finditer(html):
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not title:
            continue
        items.append({
            "url": JNET21_ARTICLE_BASE + m.group(1),
            "title": title,
            "source": "j-net21",
        })
    return items


def fetch_jgrants(keyword: str) -> list[dict[str, str]]:
    params = urlencode({
        "keyword": keyword,
        "sort": "created_date",
        "order": "DESC",
        "acceptance": "1",
    })
    url = f"{JGRANTS_ENDPOINT}?{params}"
    raw = _get(url)
    if raw is None:
        return []
    try:
        data: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARNING: jGrants JSON parse error: {exc}", file=sys.stderr)
        return []

    # API レスポンスは {"metadata": {...}, "result": [...]} 形式（実測確認済み）
    entries = data.get("result") or []
    if not isinstance(entries, list):
        print(
            f"WARNING: unexpected jGrants result type: {type(entries).__name__}",
            file=sys.stderr,
        )
        return []

    items: list[dict[str, str]] = []
    for entry in entries:
        sid = (entry.get("id") or "").strip()
        title = (entry.get("title") or entry.get("name") or "").strip()
        area = (entry.get("target_area_search") or "").strip()
        if not (sid and title):
            continue
        items.append({
            "url": JGRANTS_DETAIL_BASE + sid,
            "title": title,
            "source": "jgrants",
            "area": area,
        })
    return items


def is_jnet21_target(item: dict[str, str]) -> bool:
    title = item["title"]
    if any(ex in title for ex in REGION_EXCLUDE):
        return False
    return any(inc in title for inc in REGION_INCLUDE)


def is_jgrants_target(item: dict[str, str]) -> bool:
    area = item.get("area", "")
    # area 未設定の場合はキーワード検索で絞られているためスルー
    if not area:
        return True
    return any(inc in area for inc in JGRANTS_REGION_INCLUDE)


def collect_all() -> list[dict[str, str]]:
    """全クエリを実行し、フィルタ・重複除去済みの item リストを返す。"""
    seen_in_run: set[str] = set()
    result: list[dict[str, str]] = []

    for query in JNET21_QUERIES:
        raw_items = fetch_jnet21(query)
        print(
            f"  J-Net21 [{query}]: {len(raw_items)} raw items",
            file=sys.stderr,
        )
        for item in raw_items:
            if item["url"] in seen_in_run:
                continue
            if is_jnet21_target(item):
                seen_in_run.add(item["url"])
                result.append(item)

    for keyword in JGRANTS_QUERIES:
        raw_items = fetch_jgrants(keyword)
        print(
            f"  jGrants [{keyword}]: {len(raw_items)} raw items",
            file=sys.stderr,
        )
        for item in raw_items:
            if item["url"] in seen_in_run:
                continue
            if is_jgrants_target(item):
                seen_in_run.add(item["url"])
                result.append(item)

    return result


def build_message(new_items: list[dict[str, str]], today: date) -> str:
    lines = [f":mag: 補助金新着情報 ({today.isoformat()}) — {len(new_items)} 件"]
    for item in new_items:
        source_label = "J-Net21" if item["source"] == "j-net21" else "jGrants"
        lines.append(f"• {item['title']}")
        lines.append(f"  {source_label} | {item['url']}")
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
    parser = argparse.ArgumentParser(description="補助金情報を収集して Slack に投稿")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack 投稿と state 更新をスキップして標準出力に出す",
    )
    args = parser.parse_args()

    if not args.dry_run:
        webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook_url:
            print("ERROR: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
            return 1
    else:
        webhook_url = ""

    seen = load_seen_urls(STATE_PATH)
    all_items = collect_all()
    new_items = [item for item in all_items if item["url"] not in seen]

    print(
        f"Result: {len(all_items)} collected, {len(new_items)} new, {len(seen)} seen",
        file=sys.stderr,
    )

    if args.dry_run:
        if new_items:
            print(build_message(new_items, date.today()))
        else:
            print("(No new items)")
        return 0

    # state を更新（dry-run 以外のみ）
    save_seen_urls(STATE_PATH, seen | {item["url"] for item in all_items})

    if not new_items:
        print("No new items. Skipping Slack post.", file=sys.stderr)
        return 0

    post_to_slack(webhook_url, build_message(new_items, date.today()))
    print(f"Posted {len(new_items)} new items", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
