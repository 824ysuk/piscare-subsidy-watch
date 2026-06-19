#!/usr/bin/env python3
"""定番マスタ source_urls の公式ページを週次 fetch し、変化があれば Slack にアラートを投稿する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_PATH = REPO_ROOT / "data" / "subsidy-master.yaml"
DEFAULT_HASH_PATH = REPO_ROOT / "state" / "page_hashes.json"
MASTER_URL = "https://github.com/824ysuk/piscare-subsidy-watch/blob/main/data/subsidy-master.yaml"

_USER_AGENT = "piscare-subsidy-watch/1.0"
TIMEOUT_SECONDS = 30

_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def load_master(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"master root must be a mapping, got {type(data).__name__}")
    return data


def collect_url_to_items(master: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """source_urls を URL → items の逆引き辞書に変換する（重複 URL は 1 回 fetch で済ませる）。"""
    result: dict[str, list[dict[str, str]]] = {}
    for item in master.get("items") or []:
        name = item.get("name") or item.get("id") or ""
        item_id = item.get("id") or ""
        for url in item.get("source_urls") or []:
            if url not in result:
                result[url] = []
            result[url].append({"id": item_id, "name": name})
    return result


def load_hashes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("hashes") or {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_hashes(path: Path, hashes: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hashes": hashes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_text(url: str) -> tuple[str | None, str | None]:
    """URL を HTTP GET する。

    Returns:
        (html, None)       — 成功
        (None, "HTTP NNN") — HTTP 4xx/5xx エラー（URL 恒久障害として Slack アラート対象）
        (None, None)       — 接続エラー / タイムアウト（state 温存、アラートなし）
    """
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw: bytes = resp.read()
            content_type: str = resp.headers.get("Content-Type") or ""
        charset_match = re.search(r"charset\s*=\s*([\w-]+)", content_type, re.IGNORECASE)
        charset = charset_match.group(1) if charset_match else "utf-8"
        return raw.decode(charset, errors="replace"), None
    except HTTPError as exc:
        # HTTPError は URLError のサブクラスのため先にキャッチする
        print(f"WARNING: GET {url} HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        return None, f"HTTP {exc.code}"
    except URLError as exc:
        print(f"WARNING: GET {url} failed: {exc}", file=sys.stderr)
        return None, None


def normalize_html(html: str) -> str:
    """script・style・コメント・HTML タグを除去して plain text を返す。"""
    html = _SCRIPT_RE.sub("", html)
    html = _STYLE_RE.sub("", html)
    html = _COMMENT_RE.sub("", html)
    text = _TAG_RE.sub(" ", html)
    return _SPACE_RE.sub(" ", text).strip()


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_changes(
    url_to_items: dict[str, list[dict[str, str]]],
    prev_hashes: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    各 URL を fetch してハッシュ比較し、変化リストと今回取得したハッシュ辞書を返す。

    - fetch 成功 / old_hash 未登録: baseline 確立扱い、changes に含めない（偽警報防止）。
    - fetch 成功 / old_hash != new_hash: changes に追加。
    - HTTP 4xx/5xx: changes に "http_error" エントリとして追加（恒久 URL 障害を検知する）。
    - 接続エラー / タイムアウト: state 温存（一時障害で baseline 喪失しない）、changes に含めない。
    """
    new_hashes: dict[str, str] = {}
    changes: list[dict[str, Any]] = []

    for url, items in url_to_items.items():
        html, http_err = fetch_text(url)

        if http_err is not None:
            # HTTP 4xx/5xx: URL 恒久障害の可能性 → Slack アラート対象
            changes.append({"url": url, "items": items, "http_error": http_err})
            print(f"  HTTP ERROR: {url} ({http_err})", file=sys.stderr)
            continue

        if html is None:
            # 接続エラー / タイムアウト: state 温存、アラートなし
            continue

        new_hash = compute_hash(normalize_html(html))
        new_hashes[url] = new_hash

        old_hash = prev_hashes.get(url)
        if old_hash is None:
            print(f"  BASELINE: {url}", file=sys.stderr)
        elif old_hash != new_hash:
            changes.append({"url": url, "items": items})
            print(f"  CHANGED:  {url}", file=sys.stderr)
        else:
            print(f"  unchanged: {url}", file=sys.stderr)

    return changes, new_hashes


def build_message(changes: list[dict[str, Any]], today: date) -> str:
    lines = [
        f":warning: 公式ページ変化検知 ({today.isoformat()}) — {len(changes)} 件",
        "公式ページの内容が変わった可能性があります。確認をお願いします。",
        "",
    ]
    for change in changes:
        url = change["url"]
        names = " / ".join(item["name"] for item in change["items"])
        if "http_error" in change:
            lines.append(f"- :x: {names} (<{url}|一次情報>) — {change['http_error']} アクセス不可")
        else:
            lines.append(f"- :pencil2: {names} (<{url}|一次情報>)")
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
    parser = argparse.ArgumentParser(description="公式ページ変化検知・Slack アラート投稿")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Slack 投稿と state 更新をスキップして標準出力に出す",
    )
    parser.add_argument(
        "--hash-file",
        type=Path,
        default=DEFAULT_HASH_PATH,
        metavar="PATH",
        help="ハッシュ保存ファイルのパス（デフォルト: state/page_hashes.json）",
    )
    args = parser.parse_args()

    if not args.dry_run:
        webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook_url:
            print("ERROR: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
            return 1
    else:
        webhook_url = ""

    master = load_master(MASTER_PATH)
    url_to_items = collect_url_to_items(master)
    print(f"Loaded {len(url_to_items)} URLs from subsidy-master.yaml", file=sys.stderr)

    prev_hashes = load_hashes(args.hash_file)
    changes, new_hashes = detect_changes(url_to_items, prev_hashes)

    # fetch 失敗 URL は前回ハッシュを温存して merge
    merged_hashes = {**prev_hashes, **new_hashes}

    print(
        f"Result: {len(url_to_items)} URLs, {len(new_hashes)} fetched, "
        f"{len(changes)} changed",
        file=sys.stderr,
    )

    if args.dry_run:
        if changes:
            print(build_message(changes, date.today()))
        else:
            print("(No changes detected)")
        return 0

    save_hashes(args.hash_file, merged_hashes)

    if not changes:
        print("No changes detected. Skipping Slack post.", file=sys.stderr)
        return 0

    post_to_slack(webhook_url, build_message(changes, date.today()))
    print(f"Posted alert for {len(changes)} changed pages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
