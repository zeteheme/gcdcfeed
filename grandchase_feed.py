
#!/usr/bin/env python3

"""Build the GrandChase RSS feed and notify Discord of new posts."""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

API_URL = (
    "https://pim.playkog.com/api/website/board/gcm/NEW/en"
    "?pageIndex=1&rows=10"
)

SITE_URL = "https://grandchase.net/?category=NEW&page=1&lang=en"
POST_URL = SITE_URL
SOURCE_TZ = timezone.utc
INCLUDE_PINNED = True

FEED_TITLE = "GrandChase News (unofficial)"
FEED_DESCRIPTION = (
    "New posts from the GrandChase news page: notices, events and updates."
)

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def text(value):
    return escape(CONTROL_CHARS.sub("", str(value)))


def fetch_json(url, attempts=3):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "grandchase-rss/1.0",
            "Accept": "application/json",
        },
    )

    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(5 * attempt)

    sys.exit(f"Could not fetch GrandChase API: {last_error}")


def parse_time(value):
    return datetime.fromisoformat(value).replace(tzinfo=SOURCE_TZ)


def collect_items(data):
    items = []

    for post in data.get("datalist") or []:
        if not post.get("isUse", True):
            continue

        pinned = bool(post.get("topFixIsUse"))

        if pinned and not INCLUDE_PINNED:
            continue

        uid = str(post["boardUID"])

        items.append({
            "uid": uid,
            "title": (
                (post.get("subject") or "").strip()
                or f"Post {uid}"
            ),
            "category": post.get("categoryName") or "",
            "published": parse_time(
                post.get("displayStartDate")
                or post["lastUpdateDate"]
            ),
        })

    items.sort(
        key=lambda item: (item["published"], item["uid"]),
        reverse=True,
    )

    return items


def previous_ids(feed_path):
    """Read GUIDs from the previously published RSS feed."""

    if not feed_path.exists():
        print("No previous feed found. Establishing a baseline.")
        return None

    try:
        root = ET.parse(feed_path).getroot()
        return {
            guid.text
            for guid in root.findall(".//item/guid")
            if guid.text
        }
    except ET.ParseError as exc:
        sys.exit(f"Previous RSS feed is invalid: {exc}")


def send_discord(items):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")

    if not webhook:
        sys.exit("DISCORD_WEBHOOK_URL secret is missing.")

    if not items:
        print("No new announcements. Discord notification skipped.")
        return

    # Discord accepts up to 10 embeds per webhook message.
    for start in range(0, len(items), 10):
        batch = items[start:start + 10]

        embeds = []

        for item in batch:
            embeds.append({
                "title": item["title"][:256],
                "url": POST_URL.format(uid=item["uid"]),
                "description": (
                    f"Category: {item['category']}"
                    if item["category"]
                    else "New GrandChase announcement!"
                ),
            })

        payload = json.dumps({"embeds": embeds}).encode("utf-8")

        request = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                print(
                    f"Discord accepted notification: HTTP {response.status}"
                )
        except urllib.error.HTTPError as exc:
            print(f"Discord rejected notification: HTTP {exc.code}")
            sys.exit("Discord notification failed.")
        except Exception as exc:
            sys.exit(f"Could not contact Discord: {exc}")

    print(f"Sent {len(items)} new announcement(s) to Discord.")


def build_feed(items):
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
            lines.append(
                f"<category>{text(item['category'])}</category>"
            )

        lines.append(
            f"<description>Category: {text(item['category'])}</description>"
        )

        lines.append("</item>")

    lines += ["</channel>", "</rss>"]

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--input",
        help="Read a saved API response instead of fetching live data",
    )

    parser.add_argument(
        "--output",
        default="site/feed.xml",
        help="Where to write the RSS feed",
    )

    args = parser.parse_args()

    if args.input:
        data = json.loads(
            Path(args.input).read_text(encoding="utf-8")
        )
    else:
        data = fetch_json(API_URL)

    items = collect_items(data)

    if not items:
        sys.exit("The API returned no posts. Nothing was changed.")

    output = Path(args.output)

    # Read the old feed BEFORE replacing it.
    old_ids = previous_ids(output)

    # First run establishes a baseline without sending old announcements.
    if old_ids is None:
        new_items = []
    else:
        new_items = [
            item for item in items
            if f"gcm-{item['uid']}" not in old_ids
        ]

    print(f"Found {len(items)} GrandChase announcements.")
    print(f"Found {len(new_items)} new announcement(s).")

    # Send notifications before replacing the previous feed.
    # If Discord fails, GitHub Actions fails and can retry next run.
    if old_ids is not None:
        send_discord(new_items)

    output.parent.mkdir(parents=True, exist_ok=True)

    output.write_text(
        build_feed(items),
        encoding="utf-8",
    )

    print(f"Successfully wrote RSS feed to {output}.")


if __name__ == "__main__":
    main()
