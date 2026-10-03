#!/usr/bin/env python3
"""Validate docs/feed.xml against Apple Podcasts / Podcasting 2.0 basics.

Errors (exit 1): missing Apple-required channel/item tags, bad artwork size,
non-positive enclosure lengths, duplicate guids, invalid RFC 2822 dates,
non-dated or prerelease tags in the feed, malformed XML.
Warnings (exit 0): things to fix before submitting (e.g. owner email unset).

    python3 podcast/validate_feed.py [--feed docs/feed.xml]
        [--releases-json releases.json]   # cross-check prerelease tags
        [--check-remote]                  # range-GET each enclosure, fetch artwork
Standard library only, except artwork dimension checks use Pillow if present
(falls back to parsing the JPEG/PNG header directly).
"""
from __future__ import annotations

import argparse
import email.utils
import json
import re
import struct
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IT = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
PC = "{https://podcastindex.org/namespace/1.0}"
ATOM = "{http://www.w3.org/2005/Atom}"
PODCAST_NS_UUID = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")
DATE_TAG = re.compile(r"/releases/download/([^/]+)/")
TAG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
APPLE_CATEGORIES = {
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government", "History",
    "Health & Fitness", "Kids & Family", "Leisure", "Music", "News",
    "Religion & Spirituality", "Science", "Society & Culture", "Sports",
    "Technology", "True Crime", "TV & Film",
}
NEWS_SUBS = {"Business News", "Daily News", "Entertainment News", "News Commentary",
             "Politics", "Sports News", "Tech News"}
COMEDY_SUBS = {"Comedy Interviews", "Improv", "Stand-Up"}

errors: list[str] = []
warnings: list[str] = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def image_size(path: Path):
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            return im.size, im.mode, im.format
    except ImportError:
        pass
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return (w, h), "?", "PNG"
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            comps = data[i + 9]
            return (w, h), {1: "L", 3: "RGB", 4: "CMYK"}.get(comps, "?"), "JPEG"
        i += 2 + seg_len
    return None, None, None


def text(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def http_open(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers={"User-Agent": "newsical-feed-validator",
                                               **(headers or {})}, method=method)
    return urllib.request.urlopen(req, timeout=60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", default=str(ROOT / "docs" / "feed.xml"))
    ap.add_argument("--releases-json")
    ap.add_argument("--check-remote", action="store_true")
    args = ap.parse_args()

    show = json.loads((ROOT / "podcast" / "show.json").read_text(encoding="utf-8"))
    try:
        tree = ET.parse(args.feed)
    except ET.ParseError as e:
        print(f"ERROR: feed is not well-formed XML: {e}")
        return 1
    rss = tree.getroot()
    if rss.tag != "rss" or rss.get("version") != "2.0":
        err("root element must be <rss version=\"2.0\">")
    ch = rss.find("channel")
    if ch is None:
        print("ERROR: no <channel>")
        return 1

    # ---- channel
    for tag in ("title", "link", "description", "language"):
        if not text(ch, tag):
            err(f"channel <{tag}> missing")
    if not text(ch, f"{IT}author"):
        err("channel <itunes:author> missing")
    explicit = text(ch, f"{IT}explicit")
    if explicit not in ("true", "false"):
        err(f"channel <itunes:explicit> must be true/false, got {explicit!r}")
    if text(ch, f"{IT}type") not in ("episodic", "serial"):
        err("channel <itunes:type> must be episodic or serial")
    if len(text(ch, "description")) > 4000:
        err("channel description exceeds Apple's 4000-character limit")
    if "AI" not in text(ch, "description"):
        warn("channel description has no AI disclosure")

    cats = ch.findall(f"{IT}category")
    if not cats:
        err("channel <itunes:category> missing")
    for c in cats:
        name = c.get("text")
        if name not in APPLE_CATEGORIES:
            err(f"unknown Apple category {name!r}")
        for s in c.findall(f"{IT}category"):
            allowed = {"News": NEWS_SUBS, "Comedy": COMEDY_SUBS}.get(name, set())
            if allowed and s.get("text") not in allowed:
                err(f"unknown subcategory {s.get('text')!r} under {name!r}")

    owner = ch.find(f"{IT}owner")
    if owner is None or not text(owner, f"{IT}name"):
        err("channel <itunes:owner><itunes:name> missing")
    elif not text(owner, f"{IT}email"):
        warn("channel <itunes:owner><itunes:email> not set (show.json owner_email is a "
             "placeholder); Apple/Spotify ownership verification needs a real address")

    self_link = ch.find(f"{ATOM}link[@rel='self']")
    if self_link is None or self_link.get("href") != show["feed_url"]:
        err("atom:link rel=self missing or not equal to show.json feed_url")

    pguid = text(ch, f"{PC}guid")
    feed_no_scheme = re.sub(r"^[a-z]+://", "", show["feed_url"]).rstrip("/")
    expected = str(uuid.uuid5(PODCAST_NS_UUID, feed_no_scheme))
    if pguid != expected:
        err(f"podcast:guid {pguid!r} != UUIDv5(feed URL) {expected!r}")

    img = ch.find(f"{IT}image")
    href = img.get("href") if img is not None else None
    if not href:
        err("channel <itunes:image href> missing")
    else:
        if not href.startswith("https://"):
            err("itunes:image must be https")
        if not re.search(r"\.(jpe?g|png)$", href, re.I):
            err("itunes:image must end in .jpg/.jpeg/.png")
        local = None
        if href.startswith(show["site_url"]):
            local = ROOT / "docs" / href[len(show["site_url"]):]
        if local and local.exists():
            size, mode, fmt = image_size(local)
            kb = local.stat().st_size // 1024
            print(f"artwork: {local.relative_to(ROOT)} {size} {mode} {fmt} {kb} KB")
            if not size or size[0] != size[1] or not (1400 <= size[0] <= 3000):
                err(f"artwork must be square 1400-3000 px, got {size}")
            if mode not in ("RGB", "?"):
                err(f"artwork colour mode must be RGB, got {mode}")
            if kb > 512:
                warn(f"artwork is {kb} KB (Apple allows more, but smaller loads faster)")
        else:
            warn(f"artwork {href} not found locally; not size-checked")
        if args.check_remote:
            try:
                with http_open(href) as r:
                    print(f"remote artwork: HTTP {r.status} {r.headers.get('Content-Type')}")
            except Exception as e:  # noqa: BLE001
                warn(f"artwork not reachable yet ({e}); expected until GitHub Pages is enabled")

    # ---- items
    prerelease_tags = set()
    if args.releases_json:
        rels = json.loads(Path(args.releases_json).read_text(encoding="utf-8"))
        prerelease_tags = {r["tag_name"] for r in rels if r.get("prerelease") or r.get("draft")}

    items = ch.findall("item")
    if not items:
        warn("feed has no episodes")
    seen_guids = set()
    last_date = None
    for it in items:
        title = text(it, "title") or "(untitled)"
        label = title[:50]
        if not text(it, "title"):
            err(f"item missing <title>")
        guid = text(it, "guid")
        if not guid:
            err(f"{label}: missing <guid>")
        elif guid in seen_guids:
            err(f"{label}: duplicate guid {guid}")
        seen_guids.add(guid)

        pd = text(it, "pubDate")
        try:
            d = email.utils.parsedate_to_datetime(pd)
            if d.tzinfo is None or email.utils.format_datetime(d) != pd:
                raise ValueError("not canonical RFC 2822 with zone")
            if last_date and d > last_date:
                warn(f"{label}: items not newest-first")
            last_date = d
        except (TypeError, ValueError) as e:
            err(f"{label}: bad pubDate {pd!r} ({e})")

        enc = it.find("enclosure")
        if enc is None:
            err(f"{label}: missing <enclosure>")
            continue
        url, length, typ = enc.get("url", ""), enc.get("length", ""), enc.get("type", "")
        if not url.startswith("https://"):
            err(f"{label}: enclosure must be https")
        if not length.isdigit() or int(length) <= 0:
            err(f"{label}: enclosure length must be > 0, got {length!r}")
        if typ != "audio/mpeg":
            err(f"{label}: enclosure type {typ!r} != audio/mpeg")
        m = DATE_TAG.search(url)
        tag = m.group(1) if m else None
        if not tag or not TAG_RE.match(tag):
            err(f"{label}: enclosure tag {tag!r} is not YYYY-MM-DD (test/prerelease leak?)")
        if tag in prerelease_tags:
            err(f"{label}: prerelease/draft tag {tag} leaked into feed")

        dur = text(it, f"{IT}duration")
        if not re.fullmatch(r"\d+|\d{1,2}:\d{2}(:\d{2})?", dur):
            err(f"{label}: bad itunes:duration {dur!r}")

        tr = it.find(f"{PC}transcript")
        if tr is not None and tr.get("type") not in ("text/vtt", "application/srt",
                                                       "application/x-subrip", "text/plain",
                                                       "text/html", "application/json"):
            err(f"{label}: unexpected transcript type {tr.get('type')}")
        chp = it.find(f"{PC}chapters")
        if chp is not None:
            curl = chp.get("url", "")
            if curl.startswith(show["site_url"]):
                lp = ROOT / "docs" / curl[len(show["site_url"]):]
                if not lp.exists():
                    err(f"{label}: chapters file {lp} missing")
                else:
                    json.loads(lp.read_text(encoding="utf-8"))

        if args.check_remote:
            try:
                with http_open(url, {"Range": "bytes=0-1"}) as r:
                    total = (r.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
                    ctype = r.headers.get("Content-Type")
                    print(f"remote enclosure {tag}: HTTP {r.status} final={r.geturl()[:60]}... "
                          f"Content-Range total={total} Content-Type={ctype}")
                    if r.status != 206:
                        warn(f"{label}: enclosure host did not honour Range (HTTP {r.status})")
                    if total.isdigit() and int(total) != int(length):
                        err(f"{label}: enclosure length {length} != served size {total}")
                    if ctype and "audio" not in ctype:
                        warn(f"{label}: enclosure served as {ctype} (not audio/*); most apps "
                             "rely on the RSS type, but some validators flag it")
            except Exception as e:  # noqa: BLE001
                err(f"{label}: enclosure not reachable: {e}")

    print(f"items: {len(items)}; unique guids: {len(seen_guids)}")
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print("RESULT:", "FAIL" if errors else "PASS", f"({len(errors)} errors, {len(warnings)} warnings)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
