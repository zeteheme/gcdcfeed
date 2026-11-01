
#!/usr/bin/env python3

"""Build a multi-category GrandChase RSS feed and notify Discord."""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_API = (
    "https://pim.playkog.com/api/website/board/gcm/{category}/en"
)

DETAIL_API = (
    "https://pim.playkog.com/api/website/board/detail/{uid}"
)

CATEGORIES = ("NEW", "NOTICE", "UPDATE", "EVENT")

ROWS_PER_CATEGORY = 100

INCLUDE_PINNED = True

SITE_URL = "https://grandchase.net/"

CATEGORY_URL = (
    "https://grandchase.net/?category={category}&page=1&lang=en"
)

FEED_TITLE = "GrandChase News (unofficial)"

FEED_DESCRIPTION = (
    "Unofficial GrandChase news feed containing notices, "
    "events, and updates."
)

USER_AGENT = (
    "grandchase-rss/2.0 "
    "(https://github.com/zeteheme/gcdcfeed)"
)

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------
# HTML CONTENT EXTRACTION
# --------------------------------------------------

class ContentParser(HTMLParser):
    """Extract readable text and image URLs from HTML."""

    BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "blockquote"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.parts = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag.lower() == "img":
            src = (
                attrs.get("src")
                or attrs.get("data-src")
                or attrs.get("data-original")
            )

            if src:
                self.images.append(src.strip())

        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def plain_text(self):
        value = html.unescape("".join(self.parts))

        lines = [
            WHITESPACE.sub(" ", line).strip()
            for line in value.splitlines()
        ]

        return "\n".join(
            line for line in lines if line
        )


# --------------------------------------------------
# GENERAL HELPERS
# --------------------------------------------------

def clean_text(value):
    value = CONTROL_CHARS.sub("", str(value or ""))
    return value.strip()


def xml_safe(value):
    return escape(
        CONTROL_CHARS.sub("", str(value or ""))
    )


def parse_time(value):
    if not value:
        return datetime.now(timezone.utc)

    try:
        parsed = datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


# --------------------------------------------------
# API REQUESTS
# --------------------------------------------------

def fetch_json(url, attempts=3):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    last_error = None

    for attempt in range(1, attempts + 1):

        try:
            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:

                return json.loads(
                    response.read().decode("utf-8-sig")
                )

        except Exception as exc:

            last_error = exc

            if attempt < attempts:
                time.sleep(3 * attempt)

    raise RuntimeError(
        f"Could not fetch API: {url}\n{last_error}"
    )


# --------------------------------------------------
# COLLECT ANNOUNCEMENTS
# --------------------------------------------------

def collect_category(data, category):

    items = []

    for post in data.get("datalist") or []:

        if not isinstance(post, dict):
            continue

        if not post.get("isUse", True):
            continue

        if (
            post.get("topFixIsUse")
            and not INCLUDE_PINNED
        ):
            continue

        uid = str(
            post.get("boardUID") or ""
        ).strip()

        if not uid:
            continue

        items.append({

            "uid": uid,

            "title": (
                clean_text(post.get("subject"))
                or f"Post {uid}"
            ),

            "category": (
                clean_text(post.get("categoryName"))
                or category
            ),

            "categories": [category],

            "published": parse_time(
                post.get("displayStartDate")
                or post.get("lastUpdateDate")
            ),

            "description_text": "",

            "images": [],

        })

    return items


def collect_all_categories():

    by_uid = {}

    for category in CATEGORIES:

        url = (
            BASE_API.format(category=category)
            + f"?pageIndex=1&rows={ROWS_PER_CATEGORY}"
        )

        print(
            f"Fetching {category} announcements..."
        )

        data = fetch_json(url)

        category_posts = collect_category(
            data,
            category
        )

        for item in category_posts:

            uid = item["uid"]

            if uid in by_uid:

                if category not in by_uid[uid]["categories"]:
                    by_uid[uid]["categories"].append(category)

                continue

            by_uid[uid] = item

    items = list(by_uid.values())

    items.sort(
        key=lambda item: (
            item["published"],
            item["uid"]
        ),
        reverse=True,
    )

    return items


# --------------------------------------------------
# ANNOUNCEMENT DETAIL EXTRACTION
# --------------------------------------------------

def first_value(data, keys):

    if not isinstance(data, dict):
        return None

    for key in keys:

        value = data.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def find_detail_fields(data):

    html_keys = (
        "detailHTML",
        "contentsHTML",
        "contentHTML",
        "boardHTML",
        "html",
        "contentHtml",
        "contentsHtml",
    )

    text_keys = (
        "detailTEXT",
        "contentsTEXT",
        "contentTEXT",
        "boardTEXT",
        "content",
        "contents",
        "detailText",
        "contentText",
        "description",
    )

    queue = [data]

    while queue:

        current = queue.pop(0)

        if isinstance(current, dict):

            html_value = first_value(
                current,
                html_keys
            )

            text_value = first_value(
                current,
                text_keys
            )

            if html_value or text_value:
                return (
                    html_value or "",
                    text_value or ""
                )

            queue.extend(
                value
                for value in current.values()
                if isinstance(value, (dict, list))
            )

        elif isinstance(current, list):

            queue.extend(
                value
                for value in current
                if isinstance(value, (dict, list))
            )

    return "", ""


def get_detail(uid):

    url = DETAIL_API.format(
        uid=urllib.parse.quote(str(uid), safe="")
    )

    try:

        data = fetch_json(url)

    except Exception as exc:

        print(
            f"WARNING: Detail fetch failed for {uid}: {exc}",
            file=sys.stderr
        )

        return "", []

    raw_html, raw_text = find_detail_fields(data)

    parser = ContentParser()

    if raw_html:

        try:
            parser.feed(raw_html)
            parser.close()

        except Exception as exc:

            print(
                f"WARNING: HTML parsing failed for {uid}: {exc}",
                file=sys.stderr
            )

    body = (
        parser.plain_text()
        or clean_text(raw_text)
    )

    images = list(dict.fromkeys(
        urljoin(
            "https://grandchase.net/",
            image_url
        )
        for image_url in parser.images
    ))

    if not body:

        print(
            f"WARNING: No recognized announcement text "
            f"found for {uid}.",
            file=sys.stderr
        )

    return body, images


def enrich_items(items):

    for index, item in enumerate(items, start=1):

        print(
            f"Fetching details "
            f"{index}/{len(items)}: {item['uid']}"
        )

        body, images = get_detail(
            item["uid"]
        )

        item["description_text"] = body
        item["images"] = images


# --------------------------------------------------
# PREVIOUS FEED / DUPLICATE DETECTION
# --------------------------------------------------

def previous_ids(feed_path):

    if not feed_path.exists():

        print(
            "No previous feed found. Establishing baseline."
        )

        return None

    try:

        root = ET.parse(feed_path).getroot()

        return {
            guid.text
            for guid in root.findall(".//item/guid")
            if guid.text
        }

    except ET.ParseError as exc:

        raise RuntimeError(
            f"Previous RSS feed is invalid: {exc}"
        )


# --------------------------------------------------
# DISCORD NOTIFICATIONS
# --------------------------------------------------

def discord_description(item):

    categories = ", ".join(
        item.get("categories")
        or [item["category"]]
    )

    parts = []

    if categories:
        parts.append(
            f"**Category:** {categories}"
        )

    body = item.get("description_text") or ""

    if body:
        parts.append(body)
    else:
        parts.append(
            "_Announcement text was unavailable "
            "from the detail API._"
        )

    return "\n\n".join(parts)[:4000]


def send_discord(items):

    webhook = os.environ.get(
        "DISCORD_WEBHOOK_URL"
    )

    if not webhook:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL secret is missing."
        )

    if not items:

        print(
            "No new announcements. Discord skipped."
        )

        return

    # Keep batches below Discord's combined embed limit.
    batch = []
    batch_chars = 0

    def send_batch(embeds):

        if not embeds:
            return

        payload = json.dumps({
            "embeds": embeds
        }).encode("utf-8")

        request = urllib.request.Request(
            webhook,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:

                print(
                    "Discord accepted notification: "
                    f"HTTP {response.status}"
                )

        except urllib.error.HTTPError as exc:

            detail = exc.read().decode(
                "utf-8",
                "replace"
            )[:500]

            raise RuntimeError(
                f"Discord rejected notification: "
                f"HTTP {exc.code} {detail}"
            )

        except Exception as exc:

            raise RuntimeError(
                f"Could not contact Discord: {exc}"
            )

    for item in items:

        categories = (
            item.get("categories")
            or [item["category"]]
        )

        description = discord_description(item)

        category = categories[0]

        embed = {
            "title": item["title"][:256],

            "url": CATEGORY_URL.format(
                category=urllib.parse.quote(category)
            ),

            "description": description,

            "timestamp": item["published"].isoformat(),
        }

        if item.get("images"):

            embed["image"] = {
                "url": item["images"][0]
            }

        embed_size = (
            len(embed["title"])
            + len(embed["description"])
        )

        if (
            batch
            and (
                len(batch) >= 10
                or batch_chars + embed_size > 5500
            )
        ):

            send_batch(batch)

            batch = []
            batch_chars = 0

        batch.append(embed)
        batch_chars += embed_size

    send_batch(batch)

    print(
        f"Sent {len(items)} new announcement(s) to Discord."
    )


# --------------------------------------------------
# RSS FEED GENERATION
# --------------------------------------------------

def rss_description(item):

    categories = ", ".join(
        item.get("categories")
        or [item["category"]]
    )

    parts = []

    if categories:
        parts.append(
            f"Categories: {categories}"
        )

    if item.get("description_text"):

        parts.append(
            item["description_text"]
        )

    else:

        parts.append(
            "Announcement text unavailable from the detail API."
        )

    for image_url in item.get("images") or []:

        parts.append(
            f"Image: {image_url}"
        )

    return "\n\n".join(parts)


def build_feed(items):

    last_build = (
        items[0]["published"]
        if items
        else datetime.now(timezone.utc)
    )

    lines = [

        '<?xml version="1.0" encoding="UTF-8"?>',

        '<rss version="2.0">',

        "<channel>",

        f"<title>{xml_safe(FEED_TITLE)}</title>",

        f"<link>{xml_safe(SITE_URL)}</link>",

        f"<description>{xml_safe(FEED_DESCRIPTION)}</description>",

        "<language>en</language>",

        "<ttl>60</ttl>",

        f"<lastBuildDate>{format_datetime(last_build)}</lastBuildDate>",

    ]

    for item in items:

        categories = (
            item.get("categories")
            or [item["category"]]
        )

        category = categories[0]

        link = CATEGORY_URL.format(
            category=urllib.parse.quote(category)
        )

        lines.extend([

            "<item>",

            f"<title>{xml_safe(item['title'])}</title>",

            f"<link>{xml_safe(link)}</link>",

            f'<guid isPermaLink="false">gcm-{xml_safe(item["uid"])}</guid>',

            f"<pubDate>{format_datetime(item['published'])}</pubDate>",

        ])

        for category_name in categories:

            if category_name:

                lines.append(
                    f"<category>{xml_safe(category_name)}</category>"
                )

        lines.append(
            f"<description>{xml_safe(rss_description(item))}</description>"
        )

        lines.append("</item>")

    lines.extend([
        "</channel>",
        "</rss>",
    ])

    return "\n".join(lines) + "\n"


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--input",
        help="Optional saved API JSON response.",
    )

    parser.add_argument(
        "--output",
        default="site/feed.xml",
        help="RSS output location.",
    )

    args = parser.parse_args()

    try:

        if args.input:

            raise RuntimeError(
                "The multi-category version expects live API "
                "responses. Remove --input or adapt the input "
                "loader before using saved JSON."
            )

        items = collect_all_categories()

        if not items:

            raise RuntimeError(
                "The APIs returned no posts. "
                "The previous feed was not changed."
            )

        print(
            f"Found {len(items)} unique announcements."
        )

        enrich_items(items)

        output = Path(args.output)

        # Read the previous feed before replacing it.
        old_ids = previous_ids(output)

        if old_ids is None:

            new_items = []

        else:

            new_items = [
                item
                for item in items
                if f"gcm-{item['uid']}" not in old_ids
            ]

        print(
            f"Found {len(new_items)} new announcement(s)."
        )

        # Notify Discord before overwriting the previous feed.
        # If Discord fails, the workflow fails and can retry.
        if old_ids is not None:

            send_discord(new_items)

        output.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        output.write_text(
            build_feed(items),
            encoding="utf-8"
        )

        print(
            f"Successfully wrote RSS feed to {output}."
        )

    except Exception as exc:

        sys.exit(str(exc))


if __name__ == "__main__":
    main()
