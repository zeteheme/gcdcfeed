#!/usr/bin/env python3
"""Build a GrandChase RSS feed with announcement text, images and optional Discord alerts."""
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
DISCORD_EMBED_DESCRIPTION_LIMIT = 3900  # Leave room below Discord's 4,096-character embed limit.
MEDIA_NS = "http://search.yahoo.com/mrss/"


def xml_text(value):
    """Escape text for XML, dropping characters XML does not allow."""
    return escape(CONTROL_CHARS.sub("", str(value or "")))


class AnnouncementHTMLParser(HTMLParser):
    """Collect image URLs and turn announcement HTML into readable plain text."""
    BLOCKS = {"p", "div", "br", "li", "figure", "h1", "h2", "h3", "h4", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []
        self.in_table = False
        self.table_rows = []
        self.current_row = None
        self.current_cell = None
        self.current_cell_tag = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "table":
            self.in_table = True
            self.table_rows = []
            self.current_row = None
            self.current_cell = None
            self.parts.append("\n")
            return

        if self.in_table:
            if tag == "tr":
                self.current_row = []
            elif tag in {"td", "th"} and self.current_row is not None:
                self.current_cell = []
                self.current_cell_tag = tag
            elif tag == "img":
                src = (attrs.get("src") or "").strip()
                if src.startswith(("https://", "http://")) and src not in self.images:
                    self.images.append(src)
                alt = (attrs.get("alt") or "").strip()
                if alt and self.current_cell is not None:
                    self.current_cell.append(alt)
            return

        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "img":
            src = (attrs.get("src") or "").strip()
            if src.startswith(("https://", "http://")) and src not in self.images:
                self.images.append(src)
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self.parts.append(f" [{alt}] ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.in_table:
            if tag in {"td", "th"} and self.current_cell is not None:
                cell = re.sub(r"\s+", " ", "".join(self.current_cell)).strip()
                self.current_row.append((self.current_cell_tag, cell))
                self.current_cell = None
                self.current_cell_tag = None
            elif tag == "tr" and self.current_row is not None:
                if self.current_row:
                    self.table_rows.append(self.current_row)
                self.current_row = None
            elif tag == "table":
                self.parts.append(self._format_table(self.table_rows))
                self.parts.append("\n")
                self.in_table = False
                self.table_rows = []
            return

        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_table and self.current_cell is not None:
            self.current_cell.append(data)
        else:
            self.parts.append(data)

    @staticmethod
    def _format_table(rows):
        """Render HTML tables as labeled rows; Discord does not support Markdown tables."""
        rows = [[(kind, value) for kind, value in row] for row in rows if row]
        if not rows:
            return ""

        first_row = rows[0]
        has_header_cells = any(kind == "th" for kind, _ in first_row)
        # Some announcement tables use TD for their header row; treat the first
        # row as headings when it has multiple cells and later rows are present.
        use_first_as_header = has_header_cells or (len(rows) > 1 and len(first_row) > 1)
        if use_first_as_header:
            headers = [value or f"Column {i + 1}" for i, (_, value) in enumerate(first_row)]
            data_rows = rows[1:]
        else:
            headers = [f"Column {i + 1}" for i in range(max(len(row) for row in rows))]
            data_rows = rows

        rendered = ["**Table**"]
        for row_number, row in enumerate(data_rows, start=1):
            values = [value for _, value in row]
            fields = []
            for index, value in enumerate(values):
                if not value:
                    continue
                label = headers[index] if index < len(headers) else f"Column {index + 1}"
                fields.append(f"**{label}:** {value}")
            if fields:
                rendered.append("• " + "  |  ".join(fields))
        return "\n".join(rendered)

    def plain_text(self):
        value = html.unescape("".join(self.parts)).replace("\xa0", " ")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"[ \t]{2,}", " ", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def fetch_json(url, attempts=3):
    request = urllib.request.Request(url, headers={
        "User-Agent": "grandchase-rss/1.2 (personal feed generator)",
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
    raw_html = detail.get("detailHTML") or ""
    parser = AnnouncementHTMLParser()
    parser.feed(raw_html)

    # Use the original HTML in RSS so images and announcement layout remain inline.
    # If HTML is missing, fall back to the API's plain-text field.
    description_html = raw_html.strip()
    if not description_html:
        description_html = html.escape(detail.get("detailTEXT") or "").replace("\n", "<br>")
    plain = parser.plain_text() or (detail.get("detailTEXT") or "").strip()
    return description_html, plain, parser.images


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
            "description_html": "",
            "description_text": "",
            "images": [],
        }
        try:
            (item["description_html"],
             item["description_text"],
             item["images"]) = get_detail(uid)
        except Exception as exc:
            print(f"Warning: detail fetch failed for {uid}: {exc}", file=sys.stderr)
        items.append(item)

    items.sort(key=lambda item: (item["published"], item["uid"]), reverse=True)
    return items


def build_feed(items):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<rss version="2.0" xmlns:media="{MEDIA_NS}">',
        '<channel>',
        f"<title>{xml_text(FEED_TITLE)}</title>",
        f"<link>{xml_text(SITE_URL)}</link>",
        f"<description>{xml_text(FEED_DESCRIPTION)}</description>",
        '<language>en</language>',
        '<ttl>60</ttl>',
        f"<lastBuildDate>{format_datetime(items[0]['published'])}</lastBuildDate>",
    ]

    for item in items:
        link = POST_URL.format(uid=item["uid"])
        lines.extend([
            '<item>',
            f"<title>{xml_text(item['title'])}</title>",
            f"<link>{xml_text(link)}</link>",
            f'<guid isPermaLink="false">gcm-{xml_text(item["uid"])}</guid>',
            f"<pubDate>{format_datetime(item['published'])}</pubDate>",
        ])
        if item["category"]:
            lines.append(f"<category>{xml_text(item['category'])}</category>")

        # RSS description is escaped XML containing the original HTML.
        # Feed readers that render HTML will show images in their original positions.
        if item["description_html"]:
            lines.append(f"<description>{xml_text(item['description_html'])}</description>")
        elif item["description_text"]:
            lines.append(f"<description>{xml_text(item['description_text'])}</description>")

        # Also expose image URLs as Media RSS metadata for clients that support it.
        for image_url in item["images"]:
            lines.append(f'<media:content url="{xml_text(image_url)}" medium="image" />')

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


def discord_send(webhook_url, payload):
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "grandchase-rss/1.2"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def split_discord_text(text, limit=DISCORD_EMBED_DESCRIPTION_LIMIT):
    """Split text without dropping content, preferring paragraph/sentence boundaries."""
    text = (text or "").strip() or "(Announcement text unavailable.)"
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        # Prefer a paragraph or line break, then a sentence boundary, then a word.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            sentence_matches = list(re.finditer(r"[.!?](?:[\"'”’)]*)\s+", window))
            cut = sentence_matches[-1].end() if sentence_matches else -1
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunk = text[:cut].strip()
        if chunk:
            chunks.append(chunk)
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def discord_payloads(item):
    """Create one webhook payload per chunk; attach announcement images to the first."""
    description = item.get("description_text") or "(Announcement text unavailable.)"
    chunks = split_discord_text(description)
    total = len(chunks)
    url = POST_URL.format(uid=item["uid"])
    payloads = []

    for index, chunk in enumerate(chunks, start=1):
        title = item["title"][:256]
        if total > 1:
            suffix = f" (Part {index}/{total})"
            title = title[:256 - len(suffix)] + suffix
        embed = {"title": title, "url": url, "description": chunk}
        if item.get("category"):
            footer = f"GrandChase • {item['category']}"
            if total > 1:
                footer += f" • Part {index}/{total}"
            embed["footer"] = {"text": footer[:2048]}
        embeds = [embed]

        # Discord allows up to 10 embeds per message. Keep the first message
        # compact and attach up to three announcement images alongside its text.
        if index == 1:
            for image_url in item.get("images", [])[:3]:
                embeds.append({"url": url, "image": {"url": image_url}})
        payloads.append({"embeds": embeds})

    return payloads


def notify_discord(items, old_guids, webhook_url):
    if not webhook_url:
        return
    if old_guids is None:
        print("No previous feed found; skipping first-run Discord backlog.")
        return

    new_items = [item for item in items if f"gcm-{item['uid']}" not in old_guids]
    for item in reversed(new_items):
        try:
            payloads = discord_payloads(item)
            for part_number, payload in enumerate(payloads, start=1):
                discord_send(webhook_url, payload)
                if len(payloads) > 1:
                    print(f"Sent Discord notification for {item['uid']} (part {part_number}/{len(payloads)})")
                time.sleep(1)
            if len(payloads) == 1:
                print(f"Sent Discord notification for {item['uid']}")
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
