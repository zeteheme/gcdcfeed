#!/usr/bin/env python3

"""Build the GrandChase RSS feed and notify Discord of new announcements."""

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


# ============================================================
# CONFIGURATION
# ============================================================

BASE_API = (
    "https://pim.playkog.com/api/website/board/gcm/{category}/en"
)

DETAIL_API = (
    "https://pim.playkog.com/api/website/board/detail/{uid}"
)

CATEGORIES = ("NEW", "NOTICE", "UPDATE", "EVENT")

ROWS_PER_CATEGORY = 100
LATEST_PER_CATEGORY = 10

# Only announcements dated November 1, 2026 or later.
START_DATE = datetime(
    2026, 11, 1, 0, 0, 0,
    tzinfo=timezone.utc
)

INCLUDE_PINNED = True

SITE_URL = "https://grandchase.net/"

CATEGORY_URL = (
    "https://grandchase.net/"
    "?category={category}&page=1&lang=en"
)

FEED_TITLE = "GrandChase News (unofficial)"

FEED_DESCRIPTION = (
    "Unofficial GrandChase news feed containing "
    "notices, events, and updates."
)

USER_AGENT = (
    "grandchase-rss/2.1 "
    "(https://github.com/zeteheme/gcdcfeed)"
)

CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f]"
)

WHITESPACE = re.compile(r"\s+")

DISCORD_DESCRIPTION_LIMIT = 3900


# ============================================================
# HTML PARSER: TEXT, TABLES, AND IMAGES
# ============================================================

class ContentParser(HTMLParser):

    BLOCK_TAGS = {
        "p", "div", "br", "li", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "blockquote"
    }

    IGNORED_TAGS = {
        "script", "style", "noscript"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.parts = []
        self.images = []

        self.ignored_depth = 0

        self.in_table = False
        self.in_cell = False

        self.table_rows = []
        self.current_row = []
        self.current_cell = []

        self.link_stack = []

    def newline(self, force=False):

        tail = "".join(self.parts[-3:]).rstrip(" \t\r")

        if force or not tail.endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):

        tag = tag.lower()
        attrs = dict(attrs)

        if tag in self.IGNORED_TAGS:
            self.ignored_depth += 1
            return

        if self.ignored_depth:
            return

        if tag == "table":
            self.in_table = True
            self.table_rows = []
            self.parts.append("\n")

        elif tag == "tr" and self.in_table:
            self.current_row = []

        elif tag in ("td", "th") and self.in_table:
            self.in_cell = True
            self.current_cell = []

        elif tag == "img":

            src = (
                attrs.get("src")
                or attrs.get("data-src")
                or attrs.get("data-original")
            )

            if src:
                self.images.append(src.strip())

        elif tag == "br" and not self.in_table:
            self.newline(force=True)

        elif tag == "li" and not self.in_table:
            self.newline()
            self.parts.append("• ")

        elif tag == "a" and not self.in_table:
            href = (attrs.get("href") or "").strip()
            self.link_stack.append((href, len(self.parts)))

        elif tag in self.BLOCK_TAGS and not self.in_table:
            self.newline()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):

        if self.ignored_depth:
            return

        # Like a browser: a run of spaces/newlines is one space.
        data = re.sub(r"[ \t\r\n]+", " ", data)

        if self.in_table and self.in_cell:
            self.current_cell.append(data)
        elif not self.in_table:
            self.parts.append(data)

    def handle_endtag(self, tag):

        tag = tag.lower()

        if tag in self.IGNORED_TAGS:

            if self.ignored_depth:
                self.ignored_depth -= 1

            return

        if self.ignored_depth:
            return

        if tag in ("td", "th") and self.in_table:

            cell = WHITESPACE.sub(
                " ",
                html.unescape(
                    "".join(self.current_cell)
                )
            ).strip()

            self.current_row.append(cell)
            self.current_cell = []
            self.in_cell = False

        elif tag == "tr" and self.in_table:

            if self.current_row:
                self.table_rows.append(
                    self.current_row
                )

            self.current_row = []

        elif tag == "table" and self.in_table:

            markdown_table = self.convert_table(
                self.table_rows
            )

            if markdown_table:
                self.parts.append(
                    "\n" + markdown_table + "\n"
                )

            self.in_table = False
            self.in_cell = False
            self.table_rows = []

        elif tag == "a" and not self.in_table and self.link_stack:

            href, start = self.link_stack.pop()

            shown = "".join(self.parts[start:])

            if (
                href.lower().startswith(("http://", "https://"))
                and href not in shown
            ):
                self.parts.append(f" ({href})")

        elif (
            tag in self.BLOCK_TAGS
            and tag != "br"
            and not self.in_table
        ):
            self.newline()

    @staticmethod
    def clean_cell(value):

        value = WHITESPACE.sub(
            " ",
            str(value or "")
        ).strip()

        # Escape Markdown table separators.
        value = value.replace("|", r"\|")

        return value

    @classmethod
    def convert_table(cls, rows):

        if not rows:
            return ""

        rows = [
            [cls.clean_cell(cell) for cell in row]
            for row in rows
        ]

        rows = [
            row for row in rows
            if any(row)
        ]

        if not rows:
            return ""

        # Make every row have the same number of columns.
        column_count = max(
            len(row) for row in rows
        )

        rows = [
            row + [""] * (column_count - len(row))
            for row in rows
        ]

        # First row becomes the Markdown header.
        header = rows[0]

        markdown = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(
                ["---"] * column_count
            ) + " |",
        ]

        for row in rows[1:]:

            markdown.append(
                "| " + " | ".join(row) + " |"
            )

        return "\n".join(markdown)

    def plain_text(self):

        value = html.unescape(
            "".join(self.parts)
        )

        lines = [
            WHITESPACE.sub(" ", line).strip()
            for line in value.splitlines()
        ]

        # Keep the post's own spacing, but never more than one
        # blank line in a row.
        kept = []

        for line in lines:

            if line or (kept and kept[-1]):
                kept.append(line)

        return "\n".join(kept).strip()


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value):

    return CONTROL_CHARS.sub(
        "",
        str(value or "")
    ).strip()


def xml_safe(value):

    return escape(
        CONTROL_CHARS.sub(
            "",
            str(value or "")
        )
    )


def parse_time(value):

    if not value:
        return None

    try:

        parsed = datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):

        return None


def category_link(category):

    return CATEGORY_URL.format(
        category=urllib.parse.quote(
            category,
            safe=""
        )
    )


# ============================================================
# API REQUESTS
# ============================================================

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

            print(
                f"API attempt {attempt} failed: {exc}",
                file=sys.stderr
            )

            if attempt < attempts:
                time.sleep(3 * attempt)

    raise RuntimeError(
        f"Could not fetch API:\n{url}\n{last_error}"
    )


# ============================================================
# COLLECT ANNOUNCEMENTS
# ============================================================

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

        published = parse_time(
            post.get("displayStartDate")
            or post.get("lastUpdateDate")
        )

        # Exclude posts without a valid date.
        if published is None:
            continue

        # Exclude announcements before November 1, 2026.
        if published < START_DATE:
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

            "published": published,

            "description_text": "",

            "images": [],

        })

    # Sort newest first, then keep the newest few for this category.
    items.sort(
        key=lambda item: item["published"],
        reverse=True
    )

    return items[:LATEST_PER_CATEGORY]


def collect_all_categories():

    by_uid = {}

    for category in CATEGORIES:

        url = (
            BASE_API.format(
                category=category
            )
            + f"?pageIndex=1&rows={ROWS_PER_CATEGORY}"
        )

        print()
        print("=" * 55)
        print(f"Fetching category: {category}")
        print("=" * 55)

        data = fetch_json(url)

        category_posts = collect_category(
            data,
            category
        )

        print(
            f"Selected {len(category_posts)} "
            f"latest posts from {category}."
        )

        for item in category_posts:

            uid = item["uid"]

            if uid in by_uid:

                if category not in by_uid[uid]["categories"]:
                    by_uid[uid]["categories"].append(
                        category
                    )

                continue

            by_uid[uid] = item

    items = list(by_uid.values())

    items.sort(
        key=lambda item: item["published"],
        reverse=True
    )

    return items


# ============================================================
# FETCH FULL ANNOUNCEMENT DETAILS
# ============================================================

def get_detail(uid):

    url = DETAIL_API.format(
        uid=urllib.parse.quote(
            str(uid),
            safe=""
        )
    )

    print(
        f"Fetching announcement body: {uid}"
    )

    data = fetch_json(url)

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Unexpected detail response for {uid}."
        )

    # Confirmed GrandChase API fields.
    raw_html = data.get("detailHTML") or ""
    raw_text = data.get("detailTEXT") or ""

    parser = ContentParser()

    if raw_html:

        parser.feed(raw_html)
        parser.close()

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

        # Some posts are only an image (a poster or banner).
        body = (
            "(This post is only an image.)"
            if images
            else "(This post has no text.)"
        )

        print(
            f"Warning: announcement {uid} has no text; "
            "using a placeholder."
        )

    print(
        f"Retrieved {len(body)} characters "
        f"and {len(images)} images."
    )

    return body, images


def enrich_items(items):

    print()
    print("=" * 55)
    print("FETCHING ANNOUNCEMENT CONTENT")
    print("=" * 55)

    for index, item in enumerate(
        items,
        start=1
    ):

        print(
            f"[{index}/{len(items)}] "
            f"{item['title']}"
        )

        body, images = get_detail(
            item["uid"]
        )

        item["description_text"] = body
        item["images"] = images

    print()
    print(
        f"Successfully retrieved all {len(items)} bodies."
    )


# ============================================================
# PREVIOUS FEED / DUPLICATE DETECTION
# ============================================================

def previous_ids(feed_path):

    if not feed_path.exists():

        print(
            "No previous feed found. Establishing baseline."
        )

        return None

    try:

        root = ET.parse(
            feed_path
        ).getroot()

        return {
            guid.text
            for guid in root.findall(".//item/guid")
            if guid.text
        }

    except ET.ParseError as exc:

        raise RuntimeError(
            f"Previous RSS feed is invalid: {exc}"
        )


# ============================================================
# DISCORD MESSAGE CHUNKING
# ============================================================

def split_text(value, limit):

    value = str(value or "")

    if not value:
        return [""]

    chunks = []

    while len(value) > limit:

        split_at = value.rfind(
            "\n",
            0,
            limit
        )

        if split_at < limit // 2:

            split_at = value.rfind(
                " ",
                0,
                limit
            )

        if split_at < limit // 2:
            split_at = limit

        chunks.append(
            value[:split_at].strip()
        )

        value = value[split_at:].strip()

    if value:
        chunks.append(value)

    return chunks


def discord_descriptions(item):

    categories = item["category"]

    prefix = (
        f"**Category:** {categories}\n\n"
    )

    body = item["description_text"]

    available = (
        DISCORD_DESCRIPTION_LIMIT
        - len(prefix)
    )

    chunks = split_text(
        body,
        available
    )

    return [
        prefix + chunk
        for chunk in chunks
    ]


# ============================================================
# DISCORD NOTIFICATIONS
# ============================================================

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

    sent = 0

    for item in items:

        categories = (
            item.get("categories")
            or [item["category"]]
        )

        category = categories[0]

        descriptions = discord_descriptions(
            item
        )

        print()
        print(
            f"Sending: {item['title']}"
        )

        for index, description in enumerate(
            descriptions,
            start=1
        ):

            title = item["title"]

            if len(descriptions) > 1:

                title = (
                    f"{title} "
                    f"({index}/{len(descriptions)})"
                )

            embed = {

                "title": title[:256],

                "url": category_link(category),

                "description": description,

                "timestamp": item[
                    "published"
                ].isoformat(),

            }

            if (
                index == 1
                and item.get("images")
            ):

                embed["image"] = {
                    "url": item["images"][0]
                }

            payload = json.dumps({
                "embeds": [embed]
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
                        f"Discord accepted message: "
                        f"HTTP {response.status}"
                    )

            except urllib.error.HTTPError as exc:

                detail = exc.read().decode(
                    "utf-8",
                    "replace"
                )[:500]

                raise RuntimeError(
                    f"Discord rejected notification: "
                    f"HTTP {exc.code}: {detail}"
                )

            except Exception as exc:

                raise RuntimeError(
                    f"Could not contact Discord: {exc}"
                )

            sent += 1

            time.sleep(0.5)

    print()
    print(
        f"Sent {sent} Discord message(s) "
        f"for {len(items)} new announcement(s)."
    )


# ============================================================
# RSS DESCRIPTION
# ============================================================

def rss_description(item):

    parts = []

    if item["category"]:
        parts.append(
            "<p><strong>Category:</strong> "
            f"{html.escape(item['category'])}</p>"
        )

    for line in item["description_text"].splitlines():

        if line.strip():
            parts.append(
                f"<p>{html.escape(line)}</p>"
            )

    for image_url in item.get("images") or []:

        parts.append(
            "<p><img src="
            f"\"{html.escape(image_url, quote=True)}\""
            " alt=\"\"></p>"
        )

    return "\n".join(parts)


# ============================================================
# BUILD RSS FEED
# ============================================================

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

        link = category_link(category)

        lines.extend([

            "<item>",

            f"<title>{xml_safe(item['title'])}</title>",

            f"<link>{xml_safe(link)}</link>",

            (
                '<guid isPermaLink="false">'
                f'gcm-{xml_safe(item["uid"])}'
                '</guid>'
            ),

            f"<pubDate>{format_datetime(item['published'])}</pubDate>",

        ])

        for category_name in categories:

            if category_name:

                lines.append(
                    f"<category>{xml_safe(category_name)}</category>"
                )

        lines.append(
            "<description>"
            + xml_safe(
                rss_description(item)
            )
            + "</description>"
        )

        lines.append("</item>")

    lines.extend([
        "</channel>",
        "</rss>",
    ])

    return "\n".join(lines) + "\n"


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--output",
        default="site/feed.xml",
        help="Where to write the RSS feed."
    )

    args = parser.parse_args()

    try:

        # 1. Fetch the four categories.
        items = collect_all_categories()

        if not items:

            raise RuntimeError(
                "No announcements found on or after "
                "November 1, 2026. Previous feed unchanged."
            )

        print()
        print(
            f"Found {len(items)} unique announcements "
            "across all categories."
        )

        # 2. Retrieve every full announcement body.
        enrich_items(items)

        # 3. Read the old feed before replacing it.
        output = Path(args.output)

        old_ids = previous_ids(
            output
        )

        # 4. Identify new announcements.
        if old_ids is None:

            new_items = []

        else:

            new_items = [
                item
                for item in items
                if f"gcm-{item['uid']}" not in old_ids
            ]

        print()
        print(
            f"Found {len(new_items)} new announcement(s)."
        )

        # 5. Notify Discord before replacing the feed.
        if old_ids is not None:

            send_discord(
                sorted(
                    new_items,
                    key=lambda item: (
                        item["published"],
                        int(item["uid"])
                        if item["uid"].isdigit()
                        else 0,
                    ),
                )
            )

        # 6. Write the RSS feed.
        output.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        output.write_text(
            build_feed(items),
            encoding="utf-8"
        )

        print()
        print("=" * 55)
        print("RSS FEED SUCCESSFULLY GENERATED")
        print("=" * 55)

        print(
            f"Total announcements: {len(items)}"
        )

        print(
            f"New announcements: {len(new_items)}"
        )

        print(
            f"Date cutoff: {START_DATE.isoformat()}"
        )

        print(
            f"Maximum per category: {LATEST_PER_CATEGORY}"
        )

        print(
            f"Output: {output}"
        )

    except Exception as exc:

        print(
            f"\nERROR: {exc}",
            file=sys.stderr
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
