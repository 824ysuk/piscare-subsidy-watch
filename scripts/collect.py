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

# タイトルベース deterministic filter のための定数群。
# 設計方針: ピスケアが申請可能な制度のみを構造的に絞り込む。
# 「明示的に申請対象外と判定できるもの」のみを deny し、
# 「未知の prefix」「不明な構造」は allow に倒す（過剰 deny より signal 確保を優先）。

# Layer 1: 行政 prefix 【...】 の判定。
# 県政（県内事業者対象）は allow、他県・他市町村は deny。
PREFECTURE_ALLOW: frozenset[str] = frozenset({"福岡県"})

# Layer 1 / Layer 1b: 福岡県内の対象市町村ホワイトリスト（ピスケア申請対象）。
APPLICABLE_FUKUOKA_CITIES: frozenset[str] = frozenset({"久留米市"})

# Layer 1 / Layer 1b: 福岡県内全 29 市の完全リスト。
# Whitelist 型判定の根拠データ — 既知の福岡県市町村のみを deny 候補にし、
# 未知の市町村名様の文字列（例: 「市町村連携」「中央卸売市場」）は no-opinion で素通しする。
# 出典: 福岡県 公式市町村一覧（2026 時点・29 市）。
FUKUOKA_CITIES: frozenset[str] = frozenset({
    "福岡市", "北九州市", "久留米市", "大牟田市", "直方市",
    "飯塚市", "田川市", "柳川市", "八女市", "筑後市",
    "大川市", "行橋市", "豊前市", "中間市", "小郡市",
    "筑紫野市", "春日市", "大野城市", "宗像市", "太宰府市",
    "古賀市", "福津市", "うきは市", "宮若市", "嘉麻市",
    "朝倉市", "みやま市", "糸島市", "那珂川市",
})

# Layer 1c: 福岡県以外の都道府県 46 件。タイトル先頭近傍にあれば deny。
# 福岡県本文の県内マーカーと衝突しないよう先頭 PREFIX_SCAN_LENGTH 文字内のみスキャン。
NON_TARGET_PREFECTURES: frozenset[str] = frozenset({
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
})

PREFIX_SCAN_LENGTH: int = 12

# Layer 2: タイトル先頭の type prefix（「〇〇：」形式）で補助金本体でないものを除外する。
# 「募集」は generic な語だが、J-Net21 実データでは募集 prefix は SBIR/RFI 等の
# 非補助金情報で支配的であり、現状は deny で問題なしと判定。
# 補助金本体の「募集：〇〇補助金」形式の false negative が観測されたら見直す。
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

# 不変条件: APPLICABLE_FUKUOKA_CITIES は FUKUOKA_CITIES の部分集合でなければならない
# （ピスケアが申請対象とする市町村は福岡県内に閉じる）。
assert APPLICABLE_FUKUOKA_CITIES <= FUKUOKA_CITIES, (
    f"APPLICABLE_FUKUOKA_CITIES に FUKUOKA_CITIES 外のエントリ: "
    f"{APPLICABLE_FUKUOKA_CITIES - FUKUOKA_CITIES}"
)

_ADMIN_PREFIX_RE = re.compile(r"^【([^】]+)】")
_TYPE_PREFIX_RE = re.compile(r"^([^：:]{1,20})[：:]")

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


def _normalize_municipality(name: str) -> str:
    """「福岡県久留米市」のような県+市町村複合名から「久留米市」を抽出して正規化する。

    福岡県 prefix を取り除いて純粋な市町村名を返す。福岡県 prefix がなければそのまま返す。
    """
    for prefecture in PREFECTURE_ALLOW:
        if name.startswith(prefecture):
            return name[len(prefecture):]
    return name


def classify_admin_prefix(title: str) -> tuple[bool, str, str]:
    """タイトル先頭の【...】を解析する（Layer 1）。

    Returns:
        (allow, reason, body): body は【...】を除いた残りの文字列。
        body は Layer 1b / Layer 2 がそのまま判定に使う。
        prefix がない場合は body == title。
    """
    m = _ADMIN_PREFIX_RE.match(title)
    if not m:
        return True, "no-prefix", title
    prefix = m.group(1)
    body = title[m.end():]

    if prefix in PREFECTURE_ALLOW:
        return True, "prefecture", body
    if prefix.endswith(("県", "都", "府", "道")):
        return False, f"other-prefecture:{prefix}", body

    normalized = _normalize_municipality(prefix)
    if normalized in APPLICABLE_FUKUOKA_CITIES:
        return True, "applicable-fukuoka-city", body
    if normalized in FUKUOKA_CITIES:
        return False, f"other-fukuoka-city:{normalized}", body
    if normalized.endswith(("市", "町", "村")):
        return False, f"unknown-municipality:{normalized}", body

    return True, "unknown-prefix", body


def detect_known_municipality_at_start(text: str) -> str | None:
    """Text 先頭にある既知福岡県市町村名を返す（Layer 1b helper）。

    福岡県 prefix（「福岡県古賀市〇〇」）を剥がしてから FUKUOKA_CITIES と照合する。
    既知でなければ None を返す（他県市町村か非市町村複合語かは区別しない）。
    """
    target = _normalize_municipality(text)
    for city in FUKUOKA_CITIES:
        if target.startswith(city):
            return city
    return None


def classify_known_municipality_prefix(text: str) -> tuple[bool, str]:
    """Text 先頭の既知福岡県市町村名を判定する（Layer 1b）。

    既知福岡県市町村が APPLICABLE_FUKUOKA_CITIES にあれば allow、なければ deny。
    Whitelist 型のため未知の文字列（「市町村連携〜」「中央卸売市場〜」等）は no-opinion (allow)
    を返し、他 Layer の判定に委ねる。
    """
    city = detect_known_municipality_at_start(text)
    if city is None:
        return True, "no-known-municipality"
    if city in APPLICABLE_FUKUOKA_CITIES:
        return True, f"applicable-fukuoka-city:{city}"
    return False, f"non-applicable-fukuoka-city:{city}"


def detect_non_target_prefecture_in_prefix(title: str) -> str | None:
    """Title 先頭 PREFIX_SCAN_LENGTH 文字以内に他県マーカーがあれば返す（Layer 1c helper）。

    Body 全体ではなく先頭近傍のみスキャンするのは「東京・福岡比較セミナー」のような
    多地域比較 title での false positive を避けるため。
    """
    prefix = title[:PREFIX_SCAN_LENGTH]
    for prefecture_name in NON_TARGET_PREFECTURES:
        if prefecture_name in prefix:
            return prefecture_name
    return None


def is_type_excluded(body: str) -> bool:
    """Body 先頭の type prefix（「〇〇：」形式）が補助金本体でないか判定する（Layer 2）。

    Body は classify_admin_prefix が返した「【...】を除いた残り」を想定する。
    title 全体を渡された場合も lstrip して挙動を一致させる。
    """
    body = body.lstrip()
    m = _TYPE_PREFIX_RE.match(body)
    if not m:
        return False
    return m.group(1).strip() in TYPE_EXCLUDE


def is_industry_excluded(title: str) -> bool:
    """訪問看護と無関係な業種固有 keyword が含まれているか判定する（Layer 3）。"""
    return any(kw in title for kw in INDUSTRY_DISALLOW)


def is_title_target(title: str) -> bool:
    """タイトルベース deterministic filter（J-Net21 / jGrants 共用）。

      - Defense: TITLE_DENY_SUBSTRING の文字列を含むタイトルを排除
      - Layer 1: 行政 prefix（【...】）の解析（他県・他市町村を排除、body を抽出）
      - Layer 1b: body 先頭の既知福岡県市町村名を判定（whitelist 型）
      - Layer 1c: title 先頭近傍に他県マーカーがあれば排除
      - Layer 2: body 先頭の type prefix（「セミナー・イベント：」等）の除外
      - Layer 3: 業種固有 keyword（農業者・水産 等）の除外
    """
    if any(kw in title for kw in TITLE_DENY_SUBSTRING):
        return False

    allow, _, body = classify_admin_prefix(title)
    if not allow:
        return False

    allow, _ = classify_known_municipality_prefix(body)
    if not allow:
        return False

    if detect_non_target_prefecture_in_prefix(title) is not None:
        return False

    if is_type_excluded(body):
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
    area が空の item は jGrants 側で target_area_search が欠落しているケース。
    観測可能性のため stderr に出す（fail-closed でなく観測ログ）。
    """
    area = item.get("area", "")
    if not area:
        print(
            f"  jGrants empty target_area_search: {item['title'][:60]}",
            file=sys.stderr,
        )
    elif not any(inc in area for inc in JGRANTS_REGION_INCLUDE):
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
