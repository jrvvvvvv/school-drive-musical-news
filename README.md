# school-drive-musical-news

School Drive Musical News: a daily musical news episode (weekdays, published by 7:00 AM Eastern). The working show title is **Sloppy News Now**. The name isn't final, and it lives only in `podcast/show.json`.

The show is a satirical musical news recap for ages 13 and up. Each episode turns a few of the day's most important world and U.S. stories into short original songs and character scenes. The facts are sourced and fact-checked, and every episode publishes its source ledger and transcript (`sources.md`). The jokes and songs are satire.

**AI disclosure:** the music and host voices are AI-generated (songs by the ACE-Step music model; the hosts are synthetic voices, not imitations of real people). The scripts are written with AI assistance, and the cover art is computer-generated. The feed declares this with `<podcast:txt purpose="ai-content">true</podcast:txt>` on the channel and on every episode.

- Podcast feed (RSS): https://jrvvvvvv.github.io/school-drive-musical-news/feed.xml
- Show page: https://jrvvvvvv.github.io/school-drive-musical-news/
- Latest MP3 (stable link): https://github.com/jrvvvvvv/school-drive-musical-news/releases/latest/download/episode.mp3

The `github.io` links work only after GitHub Pages is turned on (see the owner-only steps below).

## How it works

```
Mac mini: render ──> gh release create YYYY-MM-DD (MP3s + sources.md)
                         │  (release published)
                         ▼
GitHub Actions .github/workflows/pages.yml
   build_feed.py --download: list releases, keep the newest N dated non-prerelease ones,
   GET each MP3 into _site/episodes/<DATE>.mp3, write feed.xml, index.html, chapters/
   validate_feed.py --require-audio  ──>  size check (< 900 MB)  ──>  deploy to Pages
```

- **Audio is served from GitHub Pages.** Each enclosure is `https://jrvvvvvv.github.io/school-drive-musical-news/episodes/<DATE>.mp3`, and its `length` is the exact byte count.
  - The release download URLs are not used: they redirect to 30-minute signed URLs, return 401 on `HEAD`, and serve `application/octet-stream` as an attachment.
  - The MP3s exist only in the Pages artifact. They are never committed to git (`_site/` is git-ignored).
- **Rolling window.** The feed keeps the newest `max_episodes` episodes (40, set in `show.json`). That is about 600 MB at roughly 15 MB per episode, under Pages' 1 GB limit. Both the build and the workflow fail if the site would exceed `max_site_bytes` (900 MB).
  - **Older episodes drop out of the feed and website but stay downloadable from [Releases](https://github.com/jrvvvvvv/school-drive-musical-news/releases).**
- **Only real episodes appear.** A release becomes an episode only if it is published (not a draft), not a prerelease, and its tag is exactly `YYYY-MM-DD`. Tags such as `test-2026-10-01` never reach the feed.
- **When the site rebuilds:**
  - on every push to `main`;
  - when a release is published;
  - when started by hand (`workflow_dispatch`);
  - daily at 14:45 UTC (07:45 PDT / 06:45 PST) as a backstop.
- **Stable IDs:** each episode's guid is `school-drive-musical-news-<DATE>` (`isPermaLink="false"`).
  - The show's `podcast:guid` is the standard Podcasting 2.0 UUIDv5 of the feed URL: `2bfbcbab-efef-5155-8c3f-a5c98e960865`.
  - Neither ID depends on the show title, so renaming the show doesn't change them.
- **Publish time:** `pubDate` is 07:25 America/Los_Angeles on the episode date. It is converted with `zoneinfo`, so it is correct across daylight-saving changes.
- **Episode metadata is optional.** `podcast/episodes/<DATE>.json`, or else an `episode.json` asset on the day's release (the daily pipeline uploads one), can supply `title`, `headlines`, `description`, `duration_seconds` and `chapters`. Titles focus on the day's top story (owner direction). Without it:
  - the episode title is the date;
  - the duration comes from `ffprobe` on the downloaded MP3, or is estimated at 192 kbps if `ffprobe` isn't available.
- **What every episode description includes:**
  - the same AI/satire disclosure line (`episode_ai_line` in `show.json`);
  - links to `sources.md` and the release page.
  - A `<podcast:transcript>` tag is added only when a release includes a `.vtt`, `.srt` or `.txt` asset.
- **Categories:** primary News > Daily News (Apple uses the first category), secondary Comedy.

## Files

| Path | Purpose |
|---|---|
| `podcast/show.json` | **All show metadata.** Includes the title, description (with a `{title}` placeholder), AI line, owner, categories, URLs, `max_episodes`, `max_site_bytes` and the artwork tagline. |
| `podcast/episodes/<DATE>.json` | Optional per-episode metadata. |
| `podcast/build_feed.py` | Python 3.9+, standard library only. Builds `_site/` from releases; `--download` fetches the MP3s. |
| `podcast/validate_feed.py` | Checks the built site (details below). |
| `podcast/make_artwork.py` | Pillow script that renders `site/artwork.jpg` (3000×3000 RGB) and `site/artwork.json` from `show.json`. |
| `podcast/publish_feed.sh` | Optional Mac mini step that commits episode metadata (see below). |
| `site/` | Static files copied into the Pages site: the artwork and its sidecar. |
| `.github/workflows/pages.yml` | Builds, validates, size-checks and deploys to Pages. |

`validate_feed.py` checks:
- Apple-required tags and categories;
- the AI-content flags and disclosure wording;
- that the artwork is square, 1400–3000 px, RGB, and not stale against `show.json`;
- enclosure URLs and lengths, unique guids and RFC 2822 dates;
- that no prerelease or non-dated tags leaked into the feed;
- with `--check-remote`, that `HEAD` on each enclosure returns 200 with a matching `Content-Length`.

## Commands

```bash
# Same as CI (live API, downloads audio into _site/):
python3 podcast/build_feed.py --site-dir _site --download --save-releases-json /tmp/rel.json
python3 podcast/validate_feed.py --site-dir _site --releases-json /tmp/rel.json --require-audio

# Quick preview without audio:
python3 podcast/build_feed.py --site-dir _site

# Once Pages is live, check what Apple will see:
python3 podcast/validate_feed.py --site-dir _site --check-remote

python3 podcast/make_artwork.py    # after any change to title/tagline (needs Pillow)
```

## Renaming the show

1. Edit `title` (and optionally `subtitle`, `artwork.tagline` or `artwork.wordmark`) in `podcast/show.json`.
2. Run `python3 podcast/make_artwork.py`. Multi-word names wrap onto two lines automatically.
3. Commit the change and `site/artwork.*`.

The validator fails the build if the artwork text no longer matches `show.json`. Keep `feed_url`, `podcast_guid` and `episode_guid_prefix` unchanged so subscribers are kept.

## Mac mini step (optional)

The workflow runs whenever a release is published, so the existing `gh release create` step is enough to update the feed. `publish_feed.sh` is optional. It only adds richer metadata (exact duration, headlines, chapters) as `podcast/episodes/<DATE>.json`, and the push of that file triggers another build.

To use it, clone the repo once on the Mac mini:

```bash
gh repo clone jrvvvvvv/school-drive-musical-news ~/kids-podcast/school-drive-musical-news
```

Then call it after the release command:

```bash
gh release create "$DATE" <files> -R jrvvvvvv/school-drive-musical-news --latest
~/kids-podcast/school-drive-musical-news/podcast/publish_feed.sh "$DATE" || echo "metadata step failed (episode is still published)"
```

What it does:
- Reads the duration from `ffprobe` on `~/kids-podcast/out/<DATE>/*.mp3`, falling back to `assembly_report.json`.
- Takes headlines from `~/kids-podcast/spec_<DATE>.json` (`headlines`, or scene `headline`/`title`/`id`).
- Takes chapters from `segments.json` entries that have `start` and `title`.
- Sanity-builds and validates the feed in a temp directory, then commits only the metadata file.

It is safe to re-run, never deletes anything and exits non-zero with a message on failure.

## Owner-only steps (not automated)

1. **Enable GitHub Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**. Then run the workflow once: Actions → "Build and deploy podcast site" → Run workflow, or push to `main`. Until Pages is enabled, the workflow's deploy step fails.
2. **Set the owner email:** replace `"OWNER_EMAIL_TBD"` in `podcast/show.json` with the address for Apple/Spotify ownership verification. The feed then adds `<itunes:email>` and `<podcast:locked>yes</podcast:locked>`; both are left out while the placeholder is in place.
3. **Confirm the final show name** before submitting (working title: "Sloppy News Now"; "Newsical" was dropped because it conflicts with an existing stage show). See "Renaming the show" above.
4. **Submit the feed URL:**
   - in [Apple Podcasts Connect](https://podcastsconnect.apple.com/) ("Add a show with an RSS feed");
   - in [Spotify for Creators](https://creators.spotify.com/) (add an existing podcast by RSS feed).
   - Before submitting, run `validate_feed.py --check-remote`.
