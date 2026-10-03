#!/usr/bin/env python3
"""Build the Newsical podcast RSS feed and landing page from GitHub releases.

Standard library only. Reads show metadata from podcast/show.json and optional
per-episode metadata from podcast/episodes/<YYYY-MM-DD>.json, then writes:

    docs/feed.xml              RSS 2.0 + iTunes + Podcasting 2.0 namespaces
    docs/index.html            simple landing page with audio players
    docs/chapters/<DATE>.json  only for episodes whose metadata has chapters

Only releases that are published (not draft), not prerelease, and whose tag is
exactly YYYY-MM-DD become episodes. Test tags never reach the feed.

Usage:
    python3 podcast/build_feed.py                       # live GitHub API
    python3 podcast/build_feed.py --releases-json r.json  # offline
Environment: GITHUB_TOKEN (optional) for API rate limits.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PODCAST_DIR = ROOT / "podcast"
DOCS_DIR = ROOT / "docs"

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)

TAG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLACEHOLDER_EMAIL = "OWNER_EMAIL_TBD"
TRANSCRIPT_TYPES = {
    ".vtt": "text/vtt",
    ".srt": "application/srt",
    ".txt": "text/plain",
}


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def sub(parent: ET.Element, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


# ---------------------------------------------------------------- inputs

def load_show() -> dict:
    with open(PODCAST_DIR / "show.json", encoding="utf-8") as f:
        return json.load(f)


def fetch_releases(repo: str) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    out: list[dict] = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "newsical-feed-builder",
        })
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                batch = json.load(r)
        except urllib.error.HTTPError as e:
            sys.exit(f"build_feed: GitHub API error {e.code} for {url}: {e.read()[:300]!r}")
        except urllib.error.URLError as e:
            sys.exit(f"build_feed: cannot reach GitHub API: {e.reason}")
        if not isinstance(batch, list):
            sys.exit(f"build_feed: unexpected API response: {str(batch)[:300]}")
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def load_episode_meta(date: str) -> dict:
    p = PODCAST_DIR / "episodes" / f"{date}.json"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- episodes

def pick_audio(release: dict, date: str) -> dict | None:
    assets = {a["name"]: a for a in release.get("assets", [])}
    for name in (f"school-drive-musical-news-{date}.mp3", "episode.mp3"):
        a = assets.get(name)
        if a and a.get("size", 0) > 0 and a.get("state", "uploaded") == "uploaded":
            return a
    return None


def find_asset(release: dict, predicate) -> dict | None:
    for a in release.get("assets", []):
        if predicate(a["name"]):
            return a
    return None


def episode_from_release(release: dict, show: dict) -> dict | None:
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not TAG_RE.match(tag):
        return None
    try:
        day = dt.date.fromisoformat(tag)
    except ValueError:
        return None
    audio = pick_audio(release, tag)
    if audio is None:
        print(f"build_feed: skipping {tag}: no episode MP3 asset", file=sys.stderr)
        return None

    meta = load_episode_meta(tag)
    sources = find_asset(release, lambda n: n.lower() == "sources.md") or \
        find_asset(release, lambda n: n.lower().endswith(".md"))
    transcript = find_asset(release, lambda n: Path(n).suffix.lower() in TRANSCRIPT_TYPES)

    if meta.get("duration_seconds"):
        duration = int(round(float(meta["duration_seconds"])))
        duration_estimated = False
    else:
        kbps = float(show.get("fallback_bitrate_kbps", 192))
        duration = int(round(audio["size"] * 8 / (kbps * 1000)))
        duration_estimated = True

    hh, mm = (int(x) for x in show.get("publish_time_local", "07:25").split(":"))
    tz = ZoneInfo(show.get("timezone", "America/Los_Angeles"))
    pub = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)

    date_label = f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}, {day.year}"
    headlines = [h for h in meta.get("headlines", []) if h]
    if meta.get("title"):
        title = meta["title"]
    elif headlines:
        title = f"{date_label}: " + "; ".join(headlines)
    else:
        title = date_label

    return {
        "date": tag,
        "date_label": date_label,
        "title": title,
        "headlines": headlines,
        "summary": meta.get("description", ""),
        "audio_url": audio["browser_download_url"],
        "audio_size": int(audio["size"]),
        "duration": duration,
        "duration_estimated": duration_estimated,
        "pub": pub,
        "guid": f"{show.get('episode_guid_prefix', 'newsical')}-{tag}",
        "release_url": release.get("html_url", ""),
        "sources_url": sources["browser_download_url"] if sources else "",
        "transcript": ({"url": transcript["browser_download_url"],
                        "type": TRANSCRIPT_TYPES[Path(transcript["name"]).suffix.lower()]}
                       if transcript else None),
        "chapters": meta.get("chapters") or [],
    }


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def plain_description(ep: dict, show: dict) -> str:
    parts = []
    if ep["summary"]:
        parts.append(ep["summary"])
    elif ep["headlines"]:
        parts.append("Today's stories: " + "; ".join(ep["headlines"]) + ".")
    else:
        parts.append(f"The news for {ep['date_label']}, sung.")
    parts.append(show["ai_disclosure"])
    if ep["sources_url"]:
        parts.append(f"Sources and transcript: {ep['sources_url']}")
    if ep["release_url"]:
        parts.append(f"Episode page: {ep['release_url']}")
    return "\n\n".join(parts)


def html_description(ep: dict, show: dict) -> str:
    def e(s: str) -> str:
        return html.escape(s, quote=False)

    def a(s: str) -> str:
        return html.escape(s, quote=True)
    out = []
    if ep["summary"]:
        out.append(f"<p>{e(ep['summary'])}</p>")
    if ep["headlines"]:
        out.append("<p>Today's stories:</p><ul>" +
                   "".join(f"<li>{e(h)}</li>" for h in ep["headlines"]) + "</ul>")
    if not out:
        out.append(f"<p>The news for {e(ep['date_label'])}, sung.</p>")
    out.append(f"<p><em>{e(show['ai_disclosure'])}</em></p>")
    if ep["sources_url"]:
        out.append(f'<p>Sources and transcript: <a href="{a(ep["sources_url"])}">'
                   f'{e(ep["sources_url"])}</a></p>')
    if ep["release_url"]:
        out.append(f'<p>Episode page: <a href="{a(ep["release_url"])}">{e(ep["release_url"])}</a></p>')
    return "".join(out)


def write_chapters(ep: dict, show: dict) -> str | None:
    if not ep["chapters"]:
        return None
    chapters = []
    for c in ep["chapters"]:
        start = c.get("startTime", c.get("start", c.get("start_seconds")))
        if start is None or not c.get("title"):
            continue
        chapters.append({"startTime": float(start), "title": str(c["title"])})
    if not chapters:
        return None
    chapters.sort(key=lambda c: c["startTime"])
    out_dir = DOCS_DIR / "chapters"
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {"version": "1.2.0", "chapters": chapters}
    (out_dir / f"{ep['date']}.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return show["site_url"].rstrip("/") + f"/chapters/{ep['date']}.json"


# ---------------------------------------------------------------- RSS

def build_rss(show: dict, episodes: list[dict]) -> ET.ElementTree:
    rss = ET.Element("rss", {"version": "2.0"})
    ch = sub(rss, "channel")
    sub(ch, "title", show["title"])
    sub(ch, "link", show["site_url"])
    sub(ch, "description", show["description"])
    sub(ch, "language", show["language"])
    sub(ch, "copyright", show["copyright"])
    sub(ch, "generator", "newsical build_feed.py")
    # lastBuildDate follows the newest episode so rebuilding is byte-identical.
    if episodes:
        sub(ch, "lastBuildDate", email.utils.format_datetime(episodes[0]["pub"]))
    sub(ch, q("atom", "link"), href=show["feed_url"], rel="self", type="application/rss+xml")

    img = sub(ch, "image")
    sub(img, "url", show["image_url"])
    sub(img, "title", show["title"])
    sub(img, "link", show["site_url"])

    sub(ch, q("itunes", "author"), show["author"])
    sub(ch, q("itunes", "subtitle"), show["subtitle"])
    sub(ch, q("itunes", "summary"), show["description"])
    owner = sub(ch, q("itunes", "owner"))
    sub(owner, q("itunes", "name"), show["owner_name"])
    email_addr = show.get("owner_email", "")
    if email_addr and email_addr != PLACEHOLDER_EMAIL:
        sub(owner, q("itunes", "email"), email_addr)
    sub(ch, q("itunes", "image"), href=show["image_url"])
    for cat in show["categories"]:
        # attribute is literally named "text", so pass it via attrib, not sub()'s text arg
        c = ET.SubElement(ch, q("itunes", "category"), {"text": cat["category"]})
        if cat.get("subcategory"):
            ET.SubElement(c, q("itunes", "category"), {"text": cat["subcategory"]})
    sub(ch, q("itunes", "explicit"), "true" if show["explicit"] else "false")
    sub(ch, q("itunes", "type"), show["itunes_type"])

    sub(ch, q("podcast", "guid"), show["podcast_guid"])
    sub(ch, q("podcast", "medium"), "podcast")
    if email_addr and email_addr != PLACEHOLDER_EMAIL:
        sub(ch, q("podcast", "locked"), "no", owner=email_addr)

    for ep in episodes:
        it = sub(ch, "item")
        sub(it, "title", ep["title"])
        sub(it, "description", plain_description(ep, show))
        sub(it, q("content", "encoded"), html_description(ep, show))
        if ep["release_url"]:
            sub(it, "link", ep["release_url"])
        sub(it, "guid", ep["guid"], isPermaLink="false")
        sub(it, "pubDate", email.utils.format_datetime(ep["pub"]))
        sub(it, "enclosure", url=ep["audio_url"], length=ep["audio_size"], type="audio/mpeg")
        sub(it, q("itunes", "title"), ep["title"])
        sub(it, q("itunes", "duration"), ep["duration"])
        sub(it, q("itunes", "episodeType"), "full")
        sub(it, q("itunes", "explicit"), "true" if show["explicit"] else "false")
        if ep["transcript"]:
            sub(it, q("podcast", "transcript"), url=ep["transcript"]["url"],
                type=ep["transcript"]["type"], language=show["language"])
        chapters_url = write_chapters(ep, show)
        if chapters_url:
            sub(it, q("podcast", "chapters"), url=chapters_url, type="application/json+chapters")

    ET.indent(rss, space="  ")
    return ET.ElementTree(rss)


# ---------------------------------------------------------------- HTML

PAGE_CSS = """
:root{--ink:#14181f;--paper:#f6f1e7;--muted:#5d6370;--accent:#c8392b;--rule:#d9d1c0;--card:#fffdf8}
@media (prefers-color-scheme:dark){:root{--ink:#ece6d8;--paper:#12151b;--muted:#a3a9b5;--accent:#ff6b57;--rule:#2b303a;--card:#1a1e26}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.55 Georgia,"Times New Roman",serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 64px}
header{display:flex;gap:24px;align-items:center;border-bottom:3px double var(--rule);padding-bottom:24px;flex-wrap:wrap}
header img{width:160px;height:160px;border-radius:10px;flex:none}
h1{font:800 44px/1.05 "Helvetica Neue",Arial,sans-serif;letter-spacing:.04em;margin:0 0 6px}
.sub{color:var(--muted);margin:0 0 12px}
.links a{display:inline-block;margin:0 12px 6px 0;color:var(--accent);font:600 15px/1.4 "Helvetica Neue",Arial,sans-serif}
.disclose{font-size:15px;color:var(--muted);border-left:3px solid var(--accent);padding:4px 0 4px 12px;margin:20px 0}
h2{font:700 13px/1 "Helvetica Neue",Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:32px 0 12px}
article{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:16px 18px;margin:0 0 14px}
article h3{margin:0 0 4px;font:700 19px/1.3 "Helvetica Neue",Arial,sans-serif}
.meta{color:var(--muted);font-size:14px;margin:0 0 10px}
audio{width:100%;margin:4px 0 8px}
article a{color:var(--accent)}
ul.h{margin:0 0 8px;padding-left:20px}
footer{margin-top:40px;color:var(--muted);font-size:13px}
"""


def build_index(show: dict, episodes: list[dict]) -> str:
    e = html.escape
    items = []
    for ep in episodes:
        links = []
        if ep["sources_url"]:
            links.append(f'<a href="{e(ep["sources_url"])}">Sources &amp; transcript</a>')
        links.append(f'<a href="{e(ep["audio_url"])}">Download MP3</a>')
        if ep["release_url"]:
            links.append(f'<a href="{e(ep["release_url"])}">Release page</a>')
        heads = ""
        if ep["headlines"]:
            heads = '<ul class="h">' + "".join(f"<li>{e(h)}</li>" for h in ep["headlines"]) + "</ul>"
        dur = ("~" if ep["duration_estimated"] else "") + fmt_duration(ep["duration"])
        items.append(
            f'<article id="{e(ep["date"])}"><h3>{e(ep["date_label"])}</h3>'
            f'<p class="meta">{e(dur)}</p>{heads}'
            f'<audio controls preload="none" src="{e(ep["audio_url"])}"></audio>'
            f'<p class="meta">{" &middot; ".join(links)}</p></article>'
        )
    if not items:
        items.append("<p>No episodes yet.</p>")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(show['title'])}</title>
<meta name="description" content="{e(show['subtitle'])}">
<link rel="alternate" type="application/rss+xml" title="{e(show['title'])}" href="{e(show['feed_url'])}">
<link rel="icon" href="artwork.jpg">
<style>{PAGE_CSS}</style>
</head>
<body>
<main>
<header>
<img src="artwork.jpg" alt="{e(show['title'])} cover art" width="160" height="160">
<div>
<h1>{e(show['title'])}</h1>
<p class="sub">{e(show['subtitle'])}</p>
<p class="links"><a href="{e(show['feed_url'])}">RSS feed</a></p>
</div>
</header>
<p>{e(show['description'])}</p>
<p class="disclose">{e(show.get('ai_disclosure_show', show['ai_disclosure']))}</p>
<h2>Episodes</h2>
{''.join(items)}
<footer>{e(show['copyright'])}</footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- main

def main() -> int:
    global DOCS_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--releases-json", help="read releases from this JSON file instead of the API")
    ap.add_argument("--out-dir", default=str(DOCS_DIR), help="output directory (default docs/)")
    args = ap.parse_args()

    DOCS_DIR = Path(args.out_dir).resolve()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    show = load_show()
    if args.releases_json:
        with open(args.releases_json, encoding="utf-8") as f:
            releases = json.load(f)
    else:
        releases = fetch_releases(show["github_repo"])

    episodes = [ep for ep in (episode_from_release(r, show) for r in releases) if ep]
    episodes.sort(key=lambda ep: ep["date"], reverse=True)

    tree = build_rss(show, episodes)
    feed_path = DOCS_DIR / "feed.xml"
    with open(feed_path, "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True)
        f.write(b"\n")
    (DOCS_DIR / "index.html").write_text(build_index(show, episodes), encoding="utf-8")
    nojekyll = DOCS_DIR / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("", encoding="utf-8")

    print(f"build_feed: wrote {feed_path} with {len(episodes)} episode(s): "
          + ", ".join(ep["date"] for ep in episodes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
