#!/usr/bin/env python3
"""Build a GrandChase RSS feed with full announcement text and optional Discord alerts."""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

API_URL = "https://pim.playkog.com/api/website/board/gcm/NEW/en?pageIndex=1&rows=10"
DETAIL_URL = "https://pim.playkog.com/api/website/board/detail/{uid}"
SITE_URL = "https://grandchase.net/?category=NEW&page=1&lang=en"
POST_URL = SITE_URL  # Replace if you later identify a direct announcement page URL.
SOURCE_TZ = timezone.utc
INCLUDE_PINNED = True
FEED_TITLE = "GrandChase News (unofficial)"
FEED_DESCRIPTION = "New posts from the GrandChase news page: notices, events and updates."
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
DISCORD_LIMIT = 2000


def xml_text(value):
    return escape(CONTROL_CHARS.sub("", str(value or "")))


class PlainTextHTML(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "table", "figure", "h1", "h2", "h3", "h4", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        value = html.unescape("".join(self.parts)).replace("\xa0", " ")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"[ \t]{2,}", " ", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def html_to_text(value):
    parser = PlainTextHTML()
    parser.feed(value or "")
    return parser.get_text()


def fetch_json(url, attempts=3):
    request = urllib.request.Request(url, headers={
        "User-Agent": "grandchase-rss/1.1 (personal feed generator)",
        "Accept": "application/json",
    })
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def parse_time(value):
    return datetime.fromisoformat(value).replace(tzinfo=SOURCE_TZ)


def get_detail(uid):
    detail = fetch_json(DETAIL_URL.format(uid=uid))
    # detailHTML is best for preserving paragraph/table structure; detailTEXT is fallback.
    return html_to_text(detail.get("detailHTML") or detail.get("detailTEXT") or "")


def collect_items(data):
    items = []
    for post in data.get("datalist") or []:
        if not post.get("isUse", True):
            continue
        if post.get("topFixIsUse") and not INCLUDE_PINNED:
            continue
        uid = post["boardUID"]
        item = {
            "uid": uid,
            "title": (post.get("subject") or "").strip() or f"Post {uid}",
            "category": post.get("categoryName") or "",
            "published": parse_time(post.get("displayStartDate") or post["lastUpdateDate"]),
            "description": "",
        }
        try:
            item["description"] = get_detail(uid)
        except Exception as exc:
            # A single failed detail request should not prevent the rest of the feed.
            print(f"Warning: detail fetch failed for {uid}: {exc}", file=sys.stderr)
        items.append(item)
    items.sort(key=lambda item: (item["published"], item["uid"]), reverse=True)
    return items


def build_feed(items):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0">', '<channel>',
        f"<title>{xml_text(FEED_TITLE)}</title>", f"<link>{xml_text(SITE_URL)}</link>",
        f"<description>{xml_text(FEED_DESCRIPTION)}</description>", '<language>en</language>',
        '<ttl>60</ttl>', f"<lastBuildDate>{format_datetime(items[0]['published'])}</lastBuildDate>",
    ]
    for item in items:
        link = POST_URL.format(uid=item["uid"])
        lines.extend([
            '<item>', f"<title>{xml_text(item['title'])}</title>", f"<link>{xml_text(link)}</link>",
            f'<guid isPermaLink="false">gcm-{xml_text(item["uid"])}</guid>',
            f"<pubDate>{format_datetime(item['published'])}</pubDate>",
        ])
        if item["category"]:
            lines.append(f"<category>{xml_text(item['category'])}</category>")
        if item["description"]:
            lines.append(f"<description>{xml_text(item['description'])}</description>")
        lines.append('</item>')
    lines.extend(['</channel>', '</rss>'])
    return "\n".join(lines) + "\n"


def existing_guids(feed_path):
    if not feed_path.exists():
        return None
    try:
        root = ET.fromstring(feed_path.read_text(encoding="utf-8"))
        return {node.text for node in root.findall("./channel/item/guid") if node.text}
    except (ET.ParseError, OSError):
        print("Warning: previous feed unreadable; skipping Discord notifications this run.", file=sys.stderr)
        return None


def discord_send(webhook_url, content):
    payload = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(webhook_url, data=payload, headers={
        "Content-Type": "application/json", "User-Agent": "grandchase-rss/1.1",
    }, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def discord_message(item):
    header = f"**{item['title']}**\n{POST_URL.format(uid=item['uid'])}"
    if item["category"]:
        header += f"\nCategory: {item['category']}"
    body = item["description"] or "(Announcement text unavailable.)"
    remaining = max(0, DISCORD_LIMIT - len(header) - 8)
    if len(body) > remaining:
        body = body[:max(0, remaining - 1)].rstrip() + "…"
    return f"{header}\n\n{body}"


def notify_discord(items, old_guids, webhook_url):
    if not webhook_url:
        return
    # Avoid blasting the entire existing list when setting up the webhook initially.
    if old_guids is None:
        print("No previous feed found; skipping first-run Discord backlog.")
        return
    new_items = [item for item in items if f"gcm-{item['uid']}" not in old_guids]
    for item in reversed(new_items):  # Oldest first.
        try:
            discord_send(webhook_url, discord_message(item))
            print(f"Sent Discord notification for {item['uid']}")
            time.sleep(1)
        except Exception as exc:
            print(f"Warning: Discord notification failed for {item['uid']}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="use a saved list API response instead of fetching live data")
    parser.add_argument("--output", default="site/feed.xml", help="where to write the feed")
    args = parser.parse_args()
    output = Path(args.output)
    old_guids = existing_guids(output)
    if args.input:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        data = fetch_json(API_URL)
    items = collect_items(data)
    if not items:
        sys.exit("The API returned no posts, so nothing was written.")
    notify_discord(items, old_guids, os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_feed(items), encoding="utf-8")
    print(f"Wrote {len(items)} items to {output}")


if __name__ == "__main__":
    main()
