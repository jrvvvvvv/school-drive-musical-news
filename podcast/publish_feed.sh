#!/usr/bin/env bash
# publish_feed.sh <YYYY-MM-DD>     (optional; run on the Mac mini)
#
# The GitHub Actions workflow (.github/workflows/pages.yml) rebuilds and
# deploys the feed by itself whenever a release is published, so this script
# is OPTIONAL. Its only job is to add nicer per-episode metadata (exact
# duration, headlines, chapters) as podcast/episodes/<DATE>.json; pushing that
# file to main triggers another Pages build. Without it the episode still
# appears, titled by date, with its duration measured by ffprobe in CI.
#
# Run it AFTER `gh release create <DATE> ...` has succeeded:
#   1. clone the repo into $REPO_DIR if missing (gh repo clone), else pull --rebase
#   2. confirm the release <DATE> exists and is not a draft/prerelease
#   3. write podcast/episodes/<DATE>.json if it does not already exist, from
#      ~/kids-podcast/out/<DATE>/ (ffprobe on the MP3 for exact duration,
#      assembly_report.json as fallback; chapters from segments.json entries
#      with start+title) and ~/kids-podcast/spec_<DATE>.json ("headlines" or
#      scene headline/title/id)
#   4. sanity-build the feed into a temp dir and validate it (nothing committed)
#   5. commit "episode metadata: <DATE>" (that file only) and push to main
#
# Idempotent: re-running for the same date makes no new commit. Never deletes
# files, tags or releases. Exits non-zero with a clear message on failure.
#
# Env overrides:
#   REPO_DIR   (default ~/kids-podcast/school-drive-musical-news)
#   REPO       (default jrvvvvvv/school-drive-musical-news)
#   OUT_ROOT   (default ~/kids-podcast/out)
#   SPEC_DIR   (default ~/kids-podcast)
#   PYTHON     (default python3; needs 3.9+ for zoneinfo)
#   FORCE_META=1  rewrite podcast/episodes/<DATE>.json even if it exists

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

die() { echo "publish_feed: ERROR: $*" >&2; exit 1; }
log() { echo "publish_feed: $*"; }

DATE="${1:-}"
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "usage: publish_feed.sh YYYY-MM-DD (got '${DATE}')"

REPO="${REPO:-jrvvvvvv/school-drive-musical-news}"
REPO_DIR="${REPO_DIR:-$HOME/kids-podcast/school-drive-musical-news}"
OUT_ROOT="${OUT_ROOT:-$HOME/kids-podcast/out}"
SPEC_DIR="${SPEC_DIR:-$HOME/kids-podcast}"
PYTHON="${PYTHON:-python3}"
OUT_DIR="$OUT_ROOT/$DATE"
SPEC="$SPEC_DIR/spec_$DATE.json"

command -v gh >/dev/null || die "GitHub CLI 'gh' not found on PATH"
command -v git >/dev/null || die "git not found on PATH"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found on PATH"
"$PYTHON" -c 'import sys, zoneinfo; assert sys.version_info >= (3, 9)' 2>/dev/null \
  || die "$PYTHON must be Python 3.9+ with zoneinfo"

# ---- 1. repo -------------------------------------------------------------
if [[ ! -d "$REPO_DIR/.git" ]]; then
  [[ -e "$REPO_DIR" ]] && die "$REPO_DIR exists but is not a git clone; move it aside"
  log "cloning $REPO into $REPO_DIR"
  mkdir -p "$(dirname "$REPO_DIR")"
  gh repo clone "$REPO" "$REPO_DIR" || die "gh repo clone failed"
fi
cd "$REPO_DIR"

branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || git checkout main || die "cannot switch $REPO_DIR to main"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  die "$REPO_DIR has uncommitted changes to tracked files; resolve them by hand first"
fi
git pull --rebase --quiet origin main || die "git pull --rebase failed in $REPO_DIR"

# ---- 2. release check ----------------------------------------------------
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sdmn-feed.XXXXXX")"
RELEASES_JSON="$TMP/releases.json"
gh api "repos/$REPO/releases?per_page=100" > "$RELEASES_JSON" \
  || die "could not list releases with gh api (is gh authenticated?)"

"$PYTHON" - "$RELEASES_JSON" "$DATE" <<'PY' || die "release $DATE is missing, a draft, a prerelease, or has no MP3"
import json, sys
rels = json.load(open(sys.argv[1])); date = sys.argv[2]
r = next((r for r in rels if r.get("tag_name") == date), None)
if r is None:
    sys.exit(f"no release tagged {date}")
if r.get("draft") or r.get("prerelease"):
    sys.exit(f"release {date} is draft/prerelease; it will not be in the feed")
names = {a["name"] for a in r.get("assets", [])}
if not ({"episode.mp3", f"school-drive-musical-news-{date}.mp3"} & names):
    sys.exit(f"release {date} has no episode MP3 asset")
PY

# ---- 3. episode metadata -------------------------------------------------
META="podcast/episodes/$DATE.json"
mkdir -p podcast/episodes
if [[ -f "$META" && "${FORCE_META:-0}" != "1" ]]; then
  log "keeping existing $META"
else
  MP3=""
  for cand in "$OUT_DIR/school-drive-musical-news-$DATE.mp3" "$OUT_DIR/episode.mp3"; do
    [[ -f "$cand" ]] && { MP3="$cand"; break; }
  done
  if [[ -z "$MP3" && -d "$OUT_DIR" ]]; then
    MP3="$(ls -t "$OUT_DIR"/*.mp3 2>/dev/null | head -n 1 || true)"
  fi
  DUR=""
  if [[ -n "$MP3" ]] && command -v ffprobe >/dev/null; then
    DUR="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$MP3" 2>/dev/null || true)"
  fi
  "$PYTHON" - "$META" "$OUT_DIR" "$SPEC" "$DUR" <<'PY' || die "could not write $META"
import json, os, re, sys
meta_path, out_dir, spec_path, dur = sys.argv[1:5]

def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

meta = {}
# duration: ffprobe first, then pipeline reports
try:
    if dur.strip():
        meta["duration_seconds"] = round(float(dur), 3)
except ValueError:
    pass
if "duration_seconds" not in meta:
    for name in ("assembly_report.json", "segments.json", "report.json"):
        d = load(os.path.join(out_dir, name))
        if isinstance(d, dict):
            for k in ("duration_seconds", "final_duration_s", "duration_s", "duration", "total_seconds"):
                v = d.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    meta["duration_seconds"] = round(float(v), 3)
                    break
        if "duration_seconds" in meta:
            break

# headlines: spec "headlines", else scene headline/title/id
spec = load(spec_path) or {}
heads = []
if isinstance(spec.get("headlines"), list):
    heads = [str(h).strip() for h in spec["headlines"] if str(h).strip()]
else:
    for s in spec.get("scenes", []) if isinstance(spec.get("scenes"), list) else []:
        if not isinstance(s, dict):
            continue
        h = s.get("headline") or s.get("title") or s.get("story")
        if not h and s.get("id"):
            h = re.sub(r"[_-]+", " ", str(s["id"])).strip()
            h = re.sub(r"^\d+\s*", "", h)
            h = h[:1].upper() + h[1:]
        if h and str(h).lower() not in ("intro", "outro", "preview", "cold open"):
            heads.append(str(h).strip())
if heads:
    meta["headlines"] = heads
if isinstance(spec.get("description"), str) and spec["description"].strip():
    meta["description"] = spec["description"].strip()

# chapters: only from segments that carry an explicit title and start time
segs = load(os.path.join(out_dir, "segments.json"))
if isinstance(segs, dict):
    segs = segs.get("segments")
chapters = []
if isinstance(segs, list):
    for s in segs:
        if not isinstance(s, dict):
            continue
        start = next((s[k] for k in ("start", "start_s", "start_seconds", "t0") if isinstance(s.get(k), (int, float))), None)
        title = s.get("chapter") or s.get("title") or s.get("headline")
        if start is not None and title:
            chapters.append({"startTime": round(float(start), 2), "title": str(title)})
if len(chapters) >= 2:
    meta["chapters"] = chapters

with open(meta_path, "w", encoding="utf-8") as f:
    json.dump(meta, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(f"publish_feed: wrote {meta_path}: " + ", ".join(sorted(meta)) if meta else
      f"publish_feed: wrote {meta_path} (no metadata found; duration will be estimated)")
PY
fi

# ---- 4. sanity build + validate (temp dir, no audio; nothing generated is committed)
CHECK_DIR="$TMP/site"
"$PYTHON" podcast/build_feed.py --releases-json "$RELEASES_JSON" --site-dir "$CHECK_DIR" \
  || die "build_feed.py failed; metadata not pushed"
"$PYTHON" podcast/validate_feed.py --site-dir "$CHECK_DIR" --releases-json "$RELEASES_JSON" \
  || die "validate_feed.py failed; metadata not pushed"
grep -q -- "-$DATE</guid>" "$CHECK_DIR/feed.xml" || die "episode $DATE did not make it into the feed"

# ---- 5. commit + push the metadata (the push triggers the Pages workflow) ---
git add "$META"
if git diff --cached --quiet; then
  log "metadata for $DATE already on main; nothing to commit (the release itself already triggered the Pages build)"
  exit 0
fi
git commit --quiet -m "episode metadata: $DATE" || die "git commit failed"
if ! git push --quiet origin main; then
  log "push rejected; rebasing once and retrying"
  git pull --rebase --quiet origin main || die "git pull --rebase failed after rejected push"
  git push --quiet origin main || die "git push failed"
fi
log "pushed $META; the Pages workflow will rebuild the feed"
