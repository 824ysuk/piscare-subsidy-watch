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

# J-Net21 3 層 deterministic filter（タイトルベースで noise を構造的に排除する）
#
# Layer 1: 行政 prefix（【...】）の解析でジオロケーションを判定する。
# 福岡県は県政（県内事業者対象）なので allow、久留米市は市政の allow 対象。
# 福岡県内の他市町村（宗像・北九州・福岡市等）はピスケアの場合申請対象外。
PREFECTURE_ALLOW: frozenset[str] = frozenset({"福岡県"})
MUNICIPALITY_ALLOW: frozenset[str] = frozenset({"久留米市", "福岡県久留米市"})

# Layer 2: タイトル先頭の type prefix（「〇〇：」形式）で補助金本体でないものを除外する。
# 「セミナー・イベント：〜」「専門家向け公募：〜」等。
TYPE_EXCLUDE: frozenset[str] = frozenset({
    "セミナー・イベント",
    "専門家向け公募",
    "展示会情報",
    "イベント出展者募集",
    "募集",
})

# Layer 3: 訪問看護と無関係な業種固有 keyword をタイトル中から検出して除外する。
INDUSTRY_DISALLOW: tuple[str, ...] = (
    "農業者", "畜産農家", "漁業", "水産", "林業",
    "森林", "農林水産", "農産物",
)

# Defense in depth: prefix がなくても明示除外する地名。
# 東久留米（東京都）は「久留米」クエリの誤ヒット率が高い（実測で 6 件中 4 件）。
TITLE_DENY_SUBSTRING: tuple[str, ...] = ("東久留米",)

# jGrants 地域フィルタ: target_area_search に含まれるべき文字列
JGRANTS_REGION_INCLUDE: tuple[str, ...] = ("福岡", "全国")

_ADMIN_PREFIX_RE = re.compile(r"^【([^】]+)】")
_TYPE_PREFIX_RE = re.compile(r"^([^：:]{1,20})[：:]")
# 【】なしで先頭にある市町村名（例: 「古賀市〇〇」「久留米市〇〇」「福岡県久留米市〇〇」）。
# 全角・半角の開き括弧と 【「 を含まない 2-8 文字で末尾が市町村のもの。
_BARE_MUNICIPALITY_RE = re.compile(r"^([^\s（(【「]{2,8}?[市町村])")

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


def classify_admin_prefix(title: str) -> tuple[bool, str]:
    """タイトル先頭の【...】を解析して (allow, reason) を返す（Layer 1）。

    prefix がない場合は国制度の可能性を残して allow を返す（Layer 2/3 で再判定する）。
    """
    m = _ADMIN_PREFIX_RE.match(title)
    if not m:
        return True, "no-prefix"
    prefix = m.group(1)
    if prefix in PREFECTURE_ALLOW:
        return True, "prefecture"
    if prefix.endswith(("県", "都", "府", "道")):
        return False, f"other-prefecture:{prefix}"
    if prefix.endswith(("市", "町", "村")):
        if prefix in MUNICIPALITY_ALLOW:
            return True, "municipality"
        # 「福岡県久留米市」のような県＋市町村の複合 prefix
        for pref in PREFECTURE_ALLOW:
            if prefix.startswith(pref) and prefix[len(pref):] in MUNICIPALITY_ALLOW:
                return True, "municipality-composite"
        return False, f"other-municipality:{prefix}"
    return True, "unknown-prefix"


def is_type_excluded(title: str) -> bool:
    """タイトル先頭（【...】を除いた後）の type prefix が補助金本体でないか判定する（Layer 2）。"""
    body = _ADMIN_PREFIX_RE.sub("", title).lstrip()
    m = _TYPE_PREFIX_RE.match(body)
    if not m:
        return False
    return m.group(1).strip() in TYPE_EXCLUDE


def is_industry_excluded(title: str) -> bool:
    """訪問看護と無関係な業種固有 keyword が含まれているか判定する（Layer 3）。"""
    return any(kw in title for kw in INDUSTRY_DISALLOW)


def classify_bare_municipality_prefix(title: str) -> tuple[bool, str]:
    """タイトル先頭の【】なし市町村名（例: 「古賀市〇〇」）を判定する（Layer 1b）。

    jGrants は「古賀市温室効果ガス〜」のように 【】 なしで市町村名から始まる
    タイトルが多いため、Layer 1 を補完する。検出できなければ allow を返す。
    """
    m = _BARE_MUNICIPALITY_RE.match(title)
    if not m:
        return True, "no-bare-municipality"
    bare = m.group(1)
    if bare in MUNICIPALITY_ALLOW:
        return True, "bare-municipality"
    for pref in PREFECTURE_ALLOW:
        if bare.startswith(pref) and bare[len(pref):] in MUNICIPALITY_ALLOW:
            return True, "bare-municipality-composite"
    return False, f"other-bare-municipality:{bare}"


def is_title_target(title: str) -> bool:
    """タイトルベース 4 層 deterministic filter（J-Net21 / jGrants 共用）。

      - Defense: TITLE_DENY_SUBSTRING の文字列を含むタイトルを排除
      - Layer 1: 行政 prefix（【...】）の解析（他県・他市町村を排除）
      - Layer 1b: 【】なしの市町村 prefix の判定（jGrants 向け補完）
      - Layer 2: type prefix（「セミナー・イベント：」等）の除外
      - Layer 3: 業種固有 keyword（農業者・水産 等）の除外
    """
    if any(kw in title for kw in TITLE_DENY_SUBSTRING):
        return False
    allow, _ = classify_admin_prefix(title)
    if not allow:
        return False
    allow, _ = classify_bare_municipality_prefix(title)
    if not allow:
        return False
    if is_type_excluded(title):
        return False
    if is_industry_excluded(title):
        return False
    return True


def is_jnet21_target(item: dict[str, str]) -> bool:
    return is_title_target(item["title"])


def is_jgrants_target(item: dict[str, str]) -> bool:
    """jGrants item を area + title 両方で判定する。

    area で都道府県レベル絞り込み（target_area_search が「福岡」「全国」を含む）し、
    title で 市町村・type・業種の判定を行う。
    """
    area = item.get("area", "")
    # area 設定済みかつ JGRANTS_REGION_INCLUDE に含まれなければ即 deny
    if area and not any(inc in area for inc in JGRANTS_REGION_INCLUDE):
        return False
    return is_title_target(item["title"])


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
