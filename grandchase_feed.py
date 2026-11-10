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

# One post's page on the website.
POST_URL = (
    "https://www.grandchase.net/newsdetail"
    "?category=NEW&news={uid}&lang=en"
)

# Pictures are marked in the post text as [[IMG:0]], [[IMG:1]], ...
# so they can be shown where they sit in the post.
IMAGE_MARKER = re.compile(r"\[\[IMG:(\d+)\]\]")
IMAGE_LINE = re.compile(r"^\[\[IMG:(\d+)\]\]$")

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

                if not self.in_table:

                    self.newline()

                    self.parts.append(
                        f"[[IMG:{len(self.images) - 1}]]"
                    )

                    self.newline()

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

            table_text = self.convert_table(
                self.table_rows
            )

            if table_text:
                self.parts.append(
                    "\n\n" + table_text + "\n\n"
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

        return value

    @classmethod
    def convert_table(cls, rows):

        # Discord does not draw Markdown tables, so each row becomes
        # one line, with the cells separated by " · ".
        rows = [
            [cls.clean_cell(cell) for cell in row]
            for row in rows
        ]

        rows = [
            [cell for cell in row if cell]
            for row in rows
        ]

        rows = [row for row in rows if row]

        if not rows:
            return ""

        lines = [" · ".join(row) for row in rows]

        # The first row is the table's title or header.
        if len(lines) > 1:
            lines[0] = f"**{lines[0]}**"

        return "\n".join(lines)

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


def post_link(item):

    return POST_URL.format(
        uid=urllib.parse.quote(
            str(item["uid"]),
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

    # Web addresses of the pictures, in page order, without repeats.
    images = []

    picture_numbers = {}

    for number, image_url in enumerate(parser.images):

        absolute = urljoin(
            "https://grandchase.net/",
            image_url
        )

        if absolute not in images:
            images.append(absolute)

        picture_numbers[number] = images.index(absolute)

    # Renumber the picture markers to match, dropping repeats.
    shown = set()

    def renumber(match):

        number = picture_numbers.get(int(match.group(1)))

        if number is None or number in shown:
            return ""

        shown.add(number)

        return f"[[IMG:{number}]]"

    body = IMAGE_MARKER.sub(renumber, body)

    body = re.sub(r"[ \t]+\n", "\n", body)

    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    text_only = IMAGE_MARKER.sub("", body).strip()

    if not text_only:

        # Some posts are only pictures (a poster or banner).
        placeholder = (
            "(This post is only an image.)"
            if images
            else "(This post has no text.)"
        )

        body = f"{placeholder}\n\n{body}".strip()

        print(
            f"Warning: announcement {uid} has no text; "
            "using a placeholder."
        )

    print(
        f"Retrieved {len(text_only)} characters "
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


def discord_messages(item):

    # Returns [{"text": ..., "images": [...]}, ...] in reading order.
    # The text before a picture goes in the same message as the picture,
    # and the text after it starts a new message.
    images = item.get("images") or []

    lines = (
        f"**Category:** {item['category']}\n\n"
        + item["description_text"]
    ).splitlines()

    messages = []

    text_lines = []

    pictures = []

    size = 0

    def flush():

        nonlocal text_lines, pictures, size

        text = "\n".join(text_lines).strip()

        if text or pictures:

            messages.append({
                "text": text,
                "images": pictures,
            })

        text_lines = []

        pictures = []

        size = 0

    placed = set()

    for line in lines:

        match = IMAGE_LINE.match(line.strip())

        if match:

            index = int(match.group(1))

            if index < len(images) and index not in placed:

                placed.add(index)

                if len(pictures) >= DISCORD_MAX_IMAGES:
                    flush()

                pictures.append(images[index])

            continue

        if not line.strip():

            # Blank lines only separate paragraphs.
            if text_lines and not pictures and text_lines[-1]:

                text_lines.append("")

                size += 1

            continue

        # Text after a picture starts a new message.
        if pictures:
            flush()

        for piece in split_text(
            line,
            DISCORD_DESCRIPTION_LIMIT
        ):

            if (
                text_lines
                and size + len(piece) + 1
                > DISCORD_DESCRIPTION_LIMIT
            ):
                flush()

            text_lines.append(piece)

            size += len(piece) + 1

    flush()

    # Pictures that had no spot in the text go at the end.
    leftovers = [
        url
        for index, url in enumerate(images)
        if index not in placed
    ]

    for start in range(0, len(leftovers), DISCORD_MAX_IMAGES):

        group = leftovers[start:start + DISCORD_MAX_IMAGES]

        if (
            messages
            and len(messages[-1]["images"]) + len(group)
            <= DISCORD_MAX_IMAGES
        ):

            messages[-1]["images"] = (
                messages[-1]["images"] + group
            )

        else:

            messages.append({"text": "", "images": group})

    return messages


# ============================================================
# DISCORD IMAGE UPLOADS
# ============================================================

# Some image servers refuse to let Discord load pictures from a web
# address, so the pictures are downloaded here and uploaded directly.
DISCORD_MAX_IMAGES = 4

MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def image_kind(data):

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"

    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"

    return None


def download_images(urls, referer="https://grandchase.net/"):

    files = []

    budget = MAX_UPLOAD_BYTES

    for url in urls:

        if len(files) >= DISCORD_MAX_IMAGES:
            break

        if not url.lower().startswith(("http://", "https://")):
            continue

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": referer,
                "Accept": "image/*",
            },
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:

                data = response.read(budget + 1)

        except Exception as exc:

            print(f"Could not download image {url}: {exc}")

            continue

        kind = image_kind(data)

        if kind is None or len(data) > budget:

            print(
                f"Skipping image {url}: "
                "not a usable picture, or too large."
            )

            continue

        budget -= len(data)

        content_type, extension = kind

        files.append((
            f"image{len(files) + 1}{extension}",
            content_type,
            data,
        ))

    return files


def discord_request(webhook, embeds, files=None):

    payload = json.dumps({
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    })

    if not files:

        body = payload.encode("utf-8")

        content_type = "application/json"

    else:

        boundary = "----grandchase" + os.urandom(8).hex()

        body = bytearray()

        body += (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; '
            'name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode("utf-8")

        body += payload.encode("utf-8") + b"\r\n"

        for index, (name, file_type, data) in enumerate(files):

            body += (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; '
                f'name="files[{index}]"; filename="{name}"\r\n'
                f"Content-Type: {file_type}\r\n\r\n"
            ).encode("utf-8")

            body += data + b"\r\n"

        body += f"--{boundary}--\r\n".encode("utf-8")

        body = bytes(body)

        content_type = (
            f"multipart/form-data; boundary={boundary}"
        )

    request = urllib.request.Request(
        webhook,
        data=body,
        headers={
            "Content-Type": content_type,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=60
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


def picture_embeds(embed, pictures):

    # The first picture goes in the message's own embed; each extra
    # one gets a bare embed with the same web address, which Discord
    # shows together as one picture grid.
    first = dict(embed)

    first["image"] = {"url": pictures[0]}

    return [first] + [
        {"url": embed["url"], "image": {"url": picture}}
        for picture in pictures[1:]
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

        messages = discord_messages(item)

        print()
        print(
            f"Sending: {item['title']}"
        )

        for index, message in enumerate(
            messages,
            start=1
        ):

            title = item["title"]

            if item.get("is_test"):

                title = f"[TEST] {title}"

            if len(messages) > 1:

                title = (
                    f"{title} "
                    f"({index}/{len(messages)})"
                )

            embed = {

                "title": title[:256],

                "url": post_link(item),

                "timestamp": item[
                    "published"
                ].isoformat(),

            }

            if message["text"]:

                embed["description"] = message["text"]

            embeds = [embed]

            files = []

            if message["images"]:

                files = download_images(message["images"])

                if files:

                    embeds = picture_embeds(
                        embed,
                        [
                            f"attachment://{name}"
                            for name, _, _ in files
                        ],
                    )

                else:

                    # Let Discord try to load the pictures itself.
                    embeds = picture_embeds(
                        embed,
                        message["images"]
                    )

            try:

                discord_request(webhook, embeds, files)

            except RuntimeError as exc:

                if not files:
                    raise

                print(
                    "Retrying without uploaded images "
                    f"({exc})."
                )

                discord_request(
                    webhook,
                    picture_embeds(embed, message["images"]),
                    None
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

    images = item.get("images") or []

    placed = set()

    def picture(index):

        return (
            "<p><img src="
            f"\"{html.escape(images[index], quote=True)}\""
            " alt=\"\"></p>"
        )

    for line in item["description_text"].splitlines():

        match = IMAGE_LINE.match(line.strip())

        if match:

            index = int(match.group(1))

            if index < len(images) and index not in placed:

                placed.add(index)

                parts.append(picture(index))

            continue

        if line.strip():

            safe_line = re.sub(
                r"\*\*(.+?)\*\*",
                r"<strong>\1</strong>",
                html.escape(line)
            )

            parts.append(
                f"<p>{safe_line}</p>"
            )

    for index in range(len(images)):

        if index not in placed:
            parts.append(picture(index))

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

        link = post_link(item)

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

def parse_resend_date(value):

    value = (value or "").strip()

    if not value:
        return None

    try:

        return datetime.strptime(value, "%Y-%m-%d").date()

    except ValueError:

        raise RuntimeError(
            "RESEND_DATE must look like 2026-11-06."
        )


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

        # 0. Optional test: re-send everything dated one day.
        resend_date = parse_resend_date(
            os.environ.get("RESEND_DATE")
        )

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

        # Test mode: also re-send the posts dated on that day.
        test_items = []

        if resend_date:

            new_ids = {item["uid"] for item in new_items}

            test_items = [
                item
                for item in items
                if item["published"].astimezone(
                    timezone.utc
                ).date() == resend_date
                and item["uid"] not in new_ids
            ]

            for item in test_items:
                item["is_test"] = True

            print(
                f"TEST: re-sending {len(test_items)} "
                f"announcement(s) dated {resend_date}."
            )

        # 5. Notify Discord before replacing the feed.
        if old_ids is not None or test_items:

            send_discord(
                sorted(
                    new_items + test_items,
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
