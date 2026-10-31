#!/usr/bin/env python3
"""Build an RSS 2.0 feed from the GrandChase news list.

The news page on grandchase.net loads its posts from a JSON API, so this reads
that API directly and writes a feed file. Standard library only.

    python3 grandchase_feed.py                      # fetch live data
    python3 grandchase_feed.py --input sample.json  # use a saved API response
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

# ---- Settings you may want to change ----------------------------------------
API_URL = "https://pim.playkog.com/api/website/board/gcm/NEW/en?pageIndex=1&rows=10"
SITE_URL = "https://grandchase.net/?category=NEW&page=1&lang=en"

# Where each item links. {uid} is replaced with the post's boardUID.
# Until the real post address is known, every item points at the news page.
POST_URL = SITE_URL

# The API's timestamps have no timezone. Pinned-post end times fall at 15:00,
# which is midnight in Korea, so UTC is the best guess. Change if times look off.
SOURCE_TZ = timezone.utc

# Pinned posts (boardNo 0) sit at the top of page 1 whatever their age.
INCLUDE_PINNED = True

FEED_TITLE = "GrandChase News (unofficial)"
FEED_DESCRIPTION = "New posts from the GrandChase news page: notices, events and updates."
# -----------------------------------------------------------------------------

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def text(value):
    """Escape a value for XML, dropping characters XML does not allow."""
    return escape(CONTROL_CHARS.sub("", str(value)))


def fetch_json(url, attempts=3):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "grandchase-rss/1.0 (personal feed generator)",
            "Accept": "application/json",
        },
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:  # network error, bad status, bad JSON
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)
    sys.exit(f"Could not fetch {url}: {last_error}")


def parse_time(value):
    """'2026-10-30T08:00:00' -> timezone-aware datetime."""
    return datetime.fromisoformat(value).replace(tzinfo=SOURCE_TZ)


def collect_items(data):
    items = []
    for post in data.get("datalist") or []:
        if not post.get("isUse", True):
            continue
        pinned = bool(post.get("topFixIsUse"))
        if pinned and not INCLUDE_PINNED:
            continue
        uid = post["boardUID"]
        items.append(
            {
                "uid": uid,
                "title": (post.get("subject") or "").strip() or f"Post {uid}",
                "category": post.get("categoryName") or "",
                "published": parse_time(post.get("displayStartDate") or post["lastUpdateDate"]),
            }
        )
    # Newest first; the post ID breaks ties between posts with the same timestamp.
    items.sort(key=lambda item: (item["published"], item["uid"]), reverse=True)
    return items


def build_feed(items):
    # Using the newest post's date (not "now") keeps the file identical between
    # runs when nothing has changed.
    last_build = items[0]["published"]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{text(FEED_TITLE)}</title>",
        f"<link>{text(SITE_URL)}</link>",
        f"<description>{text(FEED_DESCRIPTION)}</description>",
        "<language>en</language>",
        "<ttl>60</ttl>",
        f"<lastBuildDate>{format_datetime(last_build)}</lastBuildDate>",
    ]
    for item in items:
        link = POST_URL.format(uid=item["uid"])
        lines += [
            "<item>",
            f"<title>{text(item['title'])}</title>",
            f"<link>{text(link)}</link>",
            f'<guid isPermaLink="false">gcm-{item["uid"]}</guid>',
            f"<pubDate>{format_datetime(item['published'])}</pubDate>",
        ]
        if item["category"]:
            lines.append(f"<category>{text(item['category'])}</category>")
            lines.append(f"<description>Category: {text(item['category'])}</description>")
        lines.append("</item>")
    lines += ["</channel>", "</rss>"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", help="read this saved API response instead of fetching")
    parser.add_argument("--output", default="site/feed.xml", help="where to write the feed")
    args = parser.parse_args()

    if args.input:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        data = fetch_json(API_URL)

    items = collect_items(data)
    if not items:
        sys.exit("The API returned no posts, so nothing was written.")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_feed(items), encoding="utf-8")
    print(f"Wrote {len(items)} items to {output}")


if __name__ == "__main__":
    main()
