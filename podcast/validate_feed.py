#!/usr/bin/env python3
"""Validate the generated podcast site (default _site/feed.xml).

Errors (exit 1): malformed XML; missing Apple-required channel/item tags;
unknown categories; artwork not square 1400-3000 px RGB or stale versus
show.json; missing AI-content flags; non-positive or mismatched enclosure
lengths; duplicate guids; non-canonical RFC 2822 dates; enclosures that are not
<site_url>/episodes/YYYY-MM-DD.mp3; prerelease/draft tags in the feed.
Warnings (exit 0): things to fix before directory submission (owner email).

    python3 podcast/validate_feed.py [--site-dir _site]
        [--releases-json releases.json]  # cross-check prerelease/draft tags
        [--require-audio]                # MP3s must exist in the site dir (CI)
        [--check-remote]                 # HEAD the live URLs (once Pages is on)

--check-remote sends HEAD requests (following redirects with HEAD, as Apple
does) to every enclosure and expects 200 with Content-Length equal to the
enclosure length and an audio/* Content-Type; it also HEADs the feed and art.
Standard library only; Pillow is used for artwork checks when installed.
"""
from __future__ import annotations

import argparse
import email.utils
import json
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "podcast"))
IT = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
PC = "{https://podcastindex.org/namespace/1.0}"
ATOM = "{http://www.w3.org/2005/Atom}"
PODCAST_NS_UUID = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")
TAG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
APPLE_CATEGORIES = {
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government", "History",
    "Health & Fitness", "Kids & Family", "Leisure", "Music", "News",
    "Religion & Spirituality", "Science", "Society & Culture", "Sports",
    "Technology", "True Crime", "TV & Film",
}
SUBCATEGORIES = {
    "News": {"Business News", "Daily News", "Entertainment News", "News Commentary",
             "Politics", "Sports News", "Tech News"},
    "Comedy": {"Comedy Interviews", "Improv", "Stand-Up"},
}

errors: list = []
warnings: list = []


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
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h), {1: "L", 3: "RGB", 4: "CMYK"}.get(data[i + 9], "?"), "JPEG"
        i += 2 + seg_len
    return None, None, None


def text(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def head(url: str, max_hops: int = 5):
    """HEAD with redirects followed as HEAD. Returns (status, headers, final_url, hops)."""
    hops = []
    for _ in range(max_hops + 1):
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "podcast-feed-validator"})
        try:
            with _opener.open(req, timeout=60) as r:
                return r.status, r.headers, url, hops
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                hops.append(e.code)
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            return e.code, e.headers, url, hops
    return None, {}, url, hops


def local_path(url: str, site_url: str, site_dir: Path):
    if url.startswith(site_url):
        return site_dir / urllib.parse.unquote(url[len(site_url):])
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-dir", default=str(ROOT / "_site"))
    ap.add_argument("--releases-json")
    ap.add_argument("--require-audio", action="store_true")
    ap.add_argument("--check-remote", action="store_true")
    args = ap.parse_args()

    site_dir = Path(args.site_dir).resolve()
    show = json.loads((ROOT / "podcast" / "show.json").read_text(encoding="utf-8"))
    site_url = show["site_url"]
    if not site_url.endswith("/"):
        err("show.json site_url must end with '/'")
    feed_path = site_dir / "feed.xml"
    try:
        tree = ET.parse(feed_path)
    except (ET.ParseError, OSError) as e:
        print(f"ERROR: cannot parse {feed_path}: {e}")
        return 1
    rss = tree.getroot()
    if rss.tag != "rss" or rss.get("version") != "2.0":
        err('root element must be <rss version="2.0">')
    ch = rss.find("channel")
    if ch is None:
        print("ERROR: no <channel>")
        return 1

    # ---- channel
    if text(ch, "title") != show["title"]:
        err("channel <title> does not match show.json title")
    for tag in ("title", "link", "description", "language"):
        if not text(ch, tag):
            err(f"channel <{tag}> missing")
    if not text(ch, f"{IT}author"):
        err("channel <itunes:author> missing")
    if text(ch, f"{IT}explicit") not in ("true", "false"):
        err("channel <itunes:explicit> must be true/false")
    if text(ch, f"{IT}type") not in ("episodic", "serial"):
        err("channel <itunes:type> must be episodic or serial")
    desc = text(ch, "description")
    if len(desc) > 4000:
        err("channel description exceeds Apple's 4000-character limit")
    for needle in ("13", "fact-checked", "satire", "AI-generated", "computer-generated"):
        if needle not in desc:
            err(f"channel description is missing required disclosure wording: {needle!r}")
    if "{title}" in desc:
        err("channel description still contains the {title} placeholder")

    cats = ch.findall(f"{IT}category")
    if not cats:
        err("channel <itunes:category> missing")
    for c in cats:
        name = c.get("text")
        if name not in APPLE_CATEGORIES:
            err(f"unknown Apple category {name!r}")
        for s in c.findall(f"{IT}category"):
            if s.get("text") not in SUBCATEGORIES.get(name, set()):
                err(f"unknown subcategory {s.get('text')!r} under {name!r}")
    if cats:
        first = (cats[0].get("text"), [s.get("text") for s in cats[0].findall(f"{IT}category")])
        print(f"primary category: {first[0]} > {', '.join(first[1]) or '-'}")

    txt = [t for t in ch.findall(f"{PC}txt") if t.get("purpose") == "ai-content"]
    if not txt or (txt[0].text or "").strip() != "true":
        err('channel <podcast:txt purpose="ai-content">true</podcast:txt> missing')

    owner = ch.find(f"{IT}owner")
    locked = ch.find(f"{PC}locked")
    if owner is None or not text(owner, f"{IT}name"):
        err("channel <itunes:owner><itunes:name> missing")
    elif not text(owner, f"{IT}email"):
        warn("<itunes:owner><itunes:email> not set (owner_email in show.json is a placeholder); "
             "Apple/Spotify ownership verification needs a real address")
        if locked is not None:
            err("<podcast:locked> present without an owner email")
    else:
        if locked is None or (locked.text or "").strip() != "yes":
            err("owner email set but <podcast:locked>yes</podcast:locked> missing")

    self_link = ch.find(f"{ATOM}link[@rel='self']")
    if self_link is None or self_link.get("href") != show["feed_url"]:
        err("atom:link rel=self missing or not equal to show.json feed_url")

    pguid = text(ch, f"{PC}guid")
    feed_no_scheme = re.sub(r"^[a-z]+://", "", show["feed_url"]).rstrip("/")
    expected = str(uuid.uuid5(PODCAST_NS_UUID, feed_no_scheme))
    if pguid != expected:
        err(f"podcast:guid {pguid!r} != UUIDv5(feed URL) {expected!r}")

    # ---- artwork
    img = ch.find(f"{IT}image")
    href = img.get("href") if img is not None else None
    if not href:
        err("channel <itunes:image href> missing")
    else:
        if not href.startswith("https://") or not re.search(r"\.(jpe?g|png)$", href, re.I):
            err("itunes:image must be an https .jpg/.png URL")
        lp = local_path(href, site_url, site_dir)
        if lp and lp.exists():
            size, mode, fmt = image_size(lp)
            kb = lp.stat().st_size // 1024
            print(f"artwork: {lp.name} {size} {mode} {fmt} {kb} KB")
            if not size or size[0] != size[1] or not (1400 <= size[0] <= 3000):
                err(f"artwork must be square 1400-3000 px, got {size}")
            if mode not in ("RGB", "?"):
                err(f"artwork colour mode must be RGB, got {mode}")
            sidecar = lp.with_suffix(".json")
            try:
                from make_artwork import artwork_text  # type: ignore
            except Exception:  # noqa: BLE001  (Pillow missing etc.)
                art = show.get("artwork", {})
                want = ((art.get("wordmark") or show["title"]).upper(), art.get("tagline") or "")
            else:
                want = artwork_text(show)
            if sidecar.exists():
                got = json.loads(sidecar.read_text(encoding="utf-8"))
                if (got.get("wordmark"), got.get("tagline")) != tuple(want):
                    err(f"artwork is stale: drawn {got.get('wordmark')!r}/{got.get('tagline')!r}, "
                        f"show.json wants {want[0]!r}/{want[1]!r}; run podcast/make_artwork.py")
            else:
                warn("artwork.json sidecar missing; cannot confirm artwork matches show.json title")
        else:
            err(f"artwork {href} not found in {site_dir}")

    # ---- items
    bad_tags = set()
    if args.releases_json:
        rels = json.loads(Path(args.releases_json).read_text(encoding="utf-8"))
        bad_tags = {r["tag_name"] for r in rels if r.get("prerelease") or r.get("draft")}

    ep_prefix = site_url + show.get("episodes_path", "episodes").strip("/") + "/"
    enc_re = re.compile(re.escape(ep_prefix) + r"([^/]+)\.mp3$")
    items = ch.findall("item")
    if not items:
        warn("feed has no episodes")
    if len(items) > int(show.get("max_episodes", 40)):
        err(f"feed has {len(items)} items, more than max_episodes")
    seen = set()
    last = None
    total_audio = 0
    ai_line = show["episode_ai_line"]
    for it in items:
        title = text(it, "title")
        label = (title or "(untitled)")[:48]
        if not title:
            err("item missing <title>")
        guid = text(it, "guid")
        if not guid:
            err(f"{label}: missing <guid>")
        elif guid in seen:
            err(f"{label}: duplicate guid {guid}")
        seen.add(guid)
        if ai_line not in text(it, "description"):
            err(f"{label}: description lacks the standard AI/satire line")
        itxt = [t for t in it.findall(f"{PC}txt") if t.get("purpose") == "ai-content"]
        if not itxt or (itxt[0].text or "").strip() != "true":
            err(f"{label}: item podcast:txt ai-content missing")

        pd = text(it, "pubDate")
        try:
            d = email.utils.parsedate_to_datetime(pd)
            if d.tzinfo is None or email.utils.format_datetime(d) != pd:
                raise ValueError("not canonical RFC 2822 with zone")
            if last and d > last:
                warn(f"{label}: items not newest-first")
            last = d
        except (TypeError, ValueError) as e:
            err(f"{label}: bad pubDate {pd!r} ({e})")

        enc = it.find("enclosure")
        if enc is None:
            err(f"{label}: missing <enclosure>")
            continue
        url, length, typ = enc.get("url", ""), enc.get("length", ""), enc.get("type", "")
        if not length.isdigit() or int(length) <= 0:
            err(f"{label}: enclosure length must be > 0, got {length!r}")
            length = "0"
        if typ != "audio/mpeg":
            err(f"{label}: enclosure type {typ!r} != audio/mpeg")
        m = enc_re.match(url)
        tag = m.group(1) if m else None
        if not m:
            err(f"{label}: enclosure {url} is not under {ep_prefix}")
        elif not TAG_RE.match(tag):
            err(f"{label}: enclosure tag {tag!r} is not YYYY-MM-DD (test/prerelease leak?)")
        if tag in bad_tags:
            err(f"{label}: prerelease/draft tag {tag} leaked into feed")
        if tag and not guid.endswith(tag):
            err(f"{label}: guid {guid} does not match enclosure date {tag}")

        lp = local_path(url, site_url, site_dir)
        if lp and lp.exists():
            total_audio += lp.stat().st_size
            if lp.stat().st_size != int(length):
                err(f"{label}: local file is {lp.stat().st_size} bytes, enclosure says {length}")
        elif args.require_audio:
            err(f"{label}: {lp} missing from site dir")

        dur = text(it, f"{IT}duration")
        if not re.fullmatch(r"\d+|\d{1,2}:\d{2}(:\d{2})?", dur):
            err(f"{label}: bad itunes:duration {dur!r}")
        chp = it.find(f"{PC}chapters")
        if chp is not None:
            cp = local_path(chp.get("url", ""), site_url, site_dir)
            if not cp or not cp.exists():
                err(f"{label}: chapters file for {chp.get('url')} missing")
            else:
                json.loads(cp.read_text(encoding="utf-8"))

        if args.check_remote:
            status, headers, final, hops = head(url)
            clen = headers.get("Content-Length") if headers else None
            ctype = headers.get("Content-Type") if headers else None
            print(f"HEAD {url}: {status} after {len(hops)} redirect(s); "
                  f"Content-Length={clen} Content-Type={ctype}")
            if status != 200:
                err(f"{label}: HEAD enclosure returned {status} (Apple requires 200 on HEAD)")
            elif clen is None or int(clen) != int(length):
                err(f"{label}: HEAD Content-Length {clen} != enclosure length {length}")
            elif not (ctype or "").startswith("audio/"):
                warn(f"{label}: enclosure served as {ctype}")

    if args.check_remote:
        for u in (show["feed_url"], href):
            status, headers, _, hops = head(u)
            print(f"HEAD {u}: {status} Content-Type={headers.get('Content-Type') if headers else None}")
            if status != 200:
                err(f"HEAD {u} returned {status} (is GitHub Pages enabled and deployed?)")

    print(f"items: {len(items)}; unique guids: {len(seen)}; local audio: {total_audio / 1e6:.1f} MB")
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print("RESULT:", "FAIL" if errors else "PASS", f"({len(errors)} errors, {len(warnings)} warnings)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
