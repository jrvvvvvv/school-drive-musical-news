#!/usr/bin/env python3
"""Build the podcast site (RSS feed, landing page, chapters, audio) from GitHub releases.

Standard library only (Python 3.9+). Show metadata comes from podcast/show.json;
optional per-episode metadata from podcast/episodes/<YYYY-MM-DD>.json (missing
metadata is fine: the title falls back to the date and the duration to ffprobe
or a bitrate estimate). Static files in site/ (artwork) are copied as-is.

Output, in --site-dir (default _site/, which is git-ignored):

    feed.xml                  RSS 2.0 + iTunes + Podcasting 2.0 namespaces
    index.html                landing page with audio players
    chapters/<DATE>.json      only for episodes whose metadata has chapters
    episodes/<DATE>.mp3       only with --download (what the Pages workflow does)
    artwork.jpg, ...          copied from site/

Episodes are the newest `max_episodes` (show.json) releases that are published
(not draft), not prerelease, and tagged exactly YYYY-MM-DD. Test tags never
reach the feed. Older episodes drop out of the feed but stay in Releases.
Enclosures point at <site_url>/episodes/<DATE>.mp3 on GitHub Pages, never at
the release download URLs (those redirect to short-lived signed URLs that
fail HEAD requests).

Usage:
    python3 podcast/build_feed.py --download --save-releases-json r.json  # what CI runs
    python3 podcast/build_feed.py --releases-json r.json  # offline preview, no audio
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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PODCAST_DIR = ROOT / "podcast"
STATIC_DIR = ROOT / "site"

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
UA = "school-drive-musical-news-feed-builder"


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def sub(parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def owner_email(show: dict) -> str:
    e = (show.get("owner_email") or "").strip()
    return "" if e in ("", PLACEHOLDER_EMAIL) else e


def show_description(show: dict) -> str:
    return show["description"].replace("{title}", show["title"])


# ---------------------------------------------------------------- inputs

def load_show() -> dict:
    with open(PODCAST_DIR / "show.json", encoding="utf-8") as f:
        return json.load(f)


def fetch_releases(repo: str) -> list:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    out: list = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": UA,
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
    try:
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except ValueError as e:
        print(f"build_feed: WARNING ignoring unreadable {p}: {e}", file=sys.stderr)
        return {}


def load_release_meta(release: dict) -> dict:
    """Fallback: the daily pipeline uploads episode.json (title focused on the top story, headlines,
    description) as a release asset. A committed podcast/episodes/<DATE>.json still wins."""
    asset = next((a for a in release.get("assets", []) if a.get("name", "").lower() == "episode.json"), None)
    if not asset:
        return {}
    try:
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            meta = json.load(r)
        return meta if isinstance(meta, dict) else {}
    except (urllib.error.URLError, ValueError) as e:
        print(f"build_feed: WARNING ignoring episode.json for {release.get('tag_name')}: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------- audio

def download(url: str, dest: Path, expected: int, attempts: int = 3) -> None:
    """GET url into dest (atomic rename); verify byte count. Reuses a correct existing file."""
    if dest.exists() and dest.stat().st_size == expected:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            got = tmp.stat().st_size
            if got != expected:
                raise IOError(f"got {got} bytes, expected {expected}")
            os.replace(tmp, dest)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"build_feed: download attempt {i} failed for {url}: {e}", file=sys.stderr)
            time.sleep(3 * i)
    sys.exit(f"build_feed: could not download {url}: {last}")


def ffprobe_duration(path: Path):
    exe = shutil.which("ffprobe")
    if not exe or not path.exists():
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- episodes

def pick_audio(release: dict, date: str):
    assets = {a["name"]: a for a in release.get("assets", [])}
    for name in (f"school-drive-musical-news-{date}.mp3", "episode.mp3"):
        a = assets.get(name)
        if a and a.get("size", 0) > 0 and a.get("state", "uploaded") == "uploaded":
            return a
    return None


def find_asset(release: dict, predicate):
    for a in release.get("assets", []):
        if predicate(a["name"]):
            return a
    return None


def eligible_releases(releases: list, limit: int) -> list:
    keep = []
    for r in releases:
        tag = r.get("tag_name", "")
        if r.get("draft") or r.get("prerelease") or not TAG_RE.match(tag):
            continue
        try:
            dt.date.fromisoformat(tag)
        except ValueError:
            continue
        if pick_audio(r, tag) is None:
            print(f"build_feed: skipping {tag}: no episode MP3 asset", file=sys.stderr)
            continue
        keep.append(r)
    keep.sort(key=lambda r: r["tag_name"], reverse=True)
    if len(keep) > limit:
        print(f"build_feed: {len(keep)} episodes in Releases; feed keeps newest {limit} "
              f"(oldest kept {keep[limit - 1]['tag_name']})", file=sys.stderr)
    return keep[:limit]


def build_episode(release: dict, show: dict, site_dir: Path, do_download: bool) -> dict:
    tag = release["tag_name"]
    day = dt.date.fromisoformat(tag)
    audio = pick_audio(release, tag)
    meta = load_episode_meta(tag) or load_release_meta(release)
    sources = find_asset(release, lambda n: n.lower() == "sources.md") or \
        find_asset(release, lambda n: n.lower().endswith(".md"))
    transcript = find_asset(release, lambda n: Path(n).suffix.lower() in TRANSCRIPT_TYPES)

    ep_path = show.get("episodes_path", "episodes").strip("/")
    local_mp3 = site_dir / ep_path / f"{tag}.mp3"
    if do_download:
        download(audio["browser_download_url"], local_mp3, int(audio["size"]))
    size = local_mp3.stat().st_size if local_mp3.exists() else int(audio["size"])

    duration_source = "metadata"
    if meta.get("duration_seconds"):
        duration = float(meta["duration_seconds"])
    else:
        duration = ffprobe_duration(local_mp3)
        duration_source = "ffprobe"
        if duration is None:
            kbps = float(show.get("fallback_bitrate_kbps", 192))
            duration = size * 8 / (kbps * 1000)
            duration_source = "estimate"

    hh, mm = (int(x) for x in show.get("publish_time_local", "07:25").split(":"))
    tz = ZoneInfo(show.get("timezone", "America/Los_Angeles"))
    pub = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)

    date_label = f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}, {day.year}"
    headlines = [str(h) for h in meta.get("headlines", []) if h]
    if meta.get("title"):
        title = str(meta["title"])
    elif headlines:
        title = f"{date_label}: " + "; ".join(headlines)
    else:
        title = date_label

    return {
        "date": tag,
        "date_label": date_label,
        "title": title,
        "headlines": headlines,
        "summary": str(meta.get("description", "") or ""),
        "audio_url": show["site_url"].rstrip("/") + f"/{ep_path}/{tag}.mp3",
        "audio_size": size,
        "duration": int(round(duration)),
        "duration_source": duration_source,
        "pub": pub,
        "guid": f"{show.get('episode_guid_prefix', 'episode')}-{tag}",
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
    parts.append(show["episode_ai_line"])
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
    out.append(f"<p><em>{e(show['episode_ai_line'])}</em></p>")
    if ep["sources_url"]:
        out.append(f'<p>Sources and transcript: <a href="{a(ep["sources_url"])}">'
                   f'{e(ep["sources_url"])}</a></p>')
    if ep["release_url"]:
        out.append(f'<p>Episode page: <a href="{a(ep["release_url"])}">{e(ep["release_url"])}</a></p>')
    return "".join(out)


def write_chapters(ep: dict, show: dict, site_dir: Path):
    chapters = []
    for c in ep["chapters"]:
        if not isinstance(c, dict):
            continue
        start = c.get("startTime", c.get("start", c.get("start_seconds")))
        if start is None or not c.get("title"):
            continue
        chapters.append({"startTime": float(start), "title": str(c["title"])})
    if not chapters:
        return None
    chapters.sort(key=lambda c: c["startTime"])
    out_dir = site_dir / "chapters"
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {"version": "1.2.0", "chapters": chapters}
    (out_dir / f"{ep['date']}.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return show["site_url"].rstrip("/") + f"/chapters/{ep['date']}.json"


# ---------------------------------------------------------------- RSS

def build_rss(show: dict, episodes: list, site_dir: Path) -> ET.ElementTree:
    email_addr = owner_email(show)
    rss = ET.Element("rss", {"version": "2.0"})
    ch = sub(rss, "channel")
    sub(ch, "title", show["title"])
    sub(ch, "link", show["site_url"])
    sub(ch, "description", show_description(show))
    sub(ch, "language", show["language"])
    sub(ch, "copyright", show["copyright"])
    sub(ch, "generator", "school-drive-musical-news podcast/build_feed.py")
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
    sub(ch, q("itunes", "summary"), show_description(show))
    owner = sub(ch, q("itunes", "owner"))
    sub(owner, q("itunes", "name"), show["owner_name"])
    if email_addr:
        sub(owner, q("itunes", "email"), email_addr)
    sub(ch, q("itunes", "image"), href=show["image_url"])
    # The attribute is literally named "text"; Apple uses the first category as primary.
    for cat in show["categories"]:
        c = ET.SubElement(ch, q("itunes", "category"), {"text": cat["category"]})
        if cat.get("subcategory"):
            ET.SubElement(c, q("itunes", "category"), {"text": cat["subcategory"]})
    sub(ch, q("itunes", "explicit"), "true" if show["explicit"] else "false")
    sub(ch, q("itunes", "type"), show["itunes_type"])

    sub(ch, q("podcast", "guid"), show["podcast_guid"])
    sub(ch, q("podcast", "medium"), "podcast")
    sub(ch, q("podcast", "txt"), "true", purpose="ai-content")
    if email_addr:
        sub(ch, q("podcast", "locked"), "yes", owner=email_addr)

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
        sub(it, q("podcast", "txt"), "true", purpose="ai-content")
        if ep["transcript"]:
            sub(it, q("podcast", "transcript"), url=ep["transcript"]["url"],
                type=ep["transcript"]["type"], language=show["language"])
        chapters_url = write_chapters(ep, show, site_dir)
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


def build_index(show: dict, episodes: list) -> str:
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
        dur = ("~" if ep["duration_source"] == "estimate" else "") + fmt_duration(ep["duration"])
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
<img src="artwork.jpg" alt="{e(show['title'])} cover art (computer-generated)" width="160" height="160">
<div>
<h1>{e(show['title'])}</h1>
<p class="sub">{e(show['subtitle'])}</p>
<p class="links"><a href="{e(show['feed_url'])}">RSS feed</a></p>
</div>
</header>
<p>{e(show_description(show))}</p>
<p class="disclose">{e(show['episode_ai_line'])}</p>
<h2>Episodes</h2>
{''.join(items)}
<footer>{e(show['copyright'])}. Older episodes remain available on the
<a href="https://github.com/{e(show['github_repo'])}/releases">releases page</a>.</footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- main

def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--releases-json", help="read releases from this JSON file instead of the API")
    ap.add_argument("--site-dir", default=str(ROOT / "_site"), help="output directory (default _site/)")
    ap.add_argument("--save-releases-json", help="also write the fetched releases list here")
    ap.add_argument("--download", action="store_true",
                    help="download each episode MP3 into <site-dir>/episodes/ (what CI does)")
    args = ap.parse_args()

    site_dir = Path(args.site_dir).resolve()
    site_dir.mkdir(parents=True, exist_ok=True)
    show = load_show()

    if args.releases_json:
        with open(args.releases_json, encoding="utf-8") as f:
            releases = json.load(f)
    else:
        releases = fetch_releases(show["github_repo"])
    if args.save_releases_json:
        Path(args.save_releases_json).write_text(json.dumps(releases), encoding="utf-8")

    if STATIC_DIR.is_dir():
        shutil.copytree(STATIC_DIR, site_dir, dirs_exist_ok=True)

    limit = int(show.get("max_episodes", 40))
    chosen = eligible_releases(releases, limit)
    episodes = [build_episode(r, show, site_dir, args.download) for r in chosen]

    tree = build_rss(show, episodes, site_dir)
    feed_path = site_dir / "feed.xml"
    with open(feed_path, "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True)
        f.write(b"\n")
    (site_dir / "index.html").write_text(build_index(show, episodes), encoding="utf-8")

    total = dir_size(site_dir)
    cap = int(show.get("max_site_bytes", 900_000_000))
    print(f"build_feed: wrote {feed_path} with {len(episodes)} episode(s): "
          + ", ".join(f"{ep['date']}({ep['duration_source']})" for ep in episodes))
    print(f"build_feed: site size {total / 1e6:.1f} MB (cap {cap / 1e6:.0f} MB)")
    if total > cap:
        sys.exit(f"build_feed: site is {total} bytes, over the {cap}-byte cap; lower max_episodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
