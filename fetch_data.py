"""Download new EuroLeague games and rebuild data.json.

Run by GitHub every 20 minutes (see .github/workflows/update.yml), or by hand.
- Fetches the season's game list.
- Downloads each finished game once and keeps it in raw/ (compressed), so nothing is
  re-downloaded from scratch.
- Re-downloads games finished in the last 2 days, to pick up EuroLeague's corrections.
- Rebuilds data.json with build_data.py.

If EuroLeague's site is briefly down or slow, each download is retried a few times.
If it is still unreachable, the run ends quietly with a warning and keeps the existing
data (the next run tries again), instead of failing and sending an email.
"""
import json, os, sys, gzip, time, datetime, urllib.request
import build_data

SEASON = os.environ.get("SEASON", "E2026")
API = "https://statssupercup.euroleague.net/api"
RAW = "raw"
TRIES = 4
WAITS = [5, 15, 30]   # seconds between tries


def warn(msg):
    # "::warning::" shows as a yellow note on GitHub's run page (not a failure)
    print(f"::warning::{msg}", flush=True)


def get_once(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dubai-team-stats/1.0", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data)


def get(url):
    """Download JSON, retrying on network errors, server errors and garbled replies."""
    for i in range(TRIES):
        try:
            return get_once(url)
        except Exception as e:  # timeouts, 5xx, connection resets, half-delivered or non-JSON replies
            if i == TRIES - 1:
                raise
            print(f"  try {i + 1} failed ({type(e).__name__}: {e}); retrying in {WAITS[i]}s", flush=True)
            time.sleep(WAITS[i])


def main():
    os.makedirs(RAW, exist_ok=True)
    try:
        games = get(f"{API}/games?seasonCode={SEASON}")
        if not games.get("data"):
            raise ValueError("game list came back empty")
    except Exception as e:
        warn(f"EuroLeague game list unavailable ({type(e).__name__}: {e}). Kept existing data; will retry next run.")
        return 0
    json.dump(games, open(os.path.join(RAW, "games.json"), "w"))
    now = datetime.datetime.now(datetime.timezone.utc)
    fetched, missed = 0, []
    for g in games["data"]:
        if not g.get("played"):
            continue
        fn = os.path.join(RAW, f"b{g['gameCode']}.json.gz")
        when = datetime.datetime.fromisoformat(g["utcDate"].replace("Z", "+00:00"))
        recent = (now - when).total_seconds() < 2 * 86400
        if os.path.exists(fn) and not recent:
            continue
        try:
            b = get(f"{API}/bundle?gamecode={g['gameCode']}&seasoncode={SEASON}")
        except Exception as e:
            missed.append(g["gameCode"])
            print(f"  game {g['gameCode']}: download failed ({type(e).__name__}: {e})", flush=True)
            continue
        if not (b.get("pbp") and b.get("box")):
            continue
        tmp = fn + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(b, f, separators=(",", ":"))
        os.replace(tmp, fn)   # only replace the saved copy once the new one is complete
        fetched += 1
        time.sleep(0.5)  # be polite to EuroLeague's servers
    print(f"downloaded {fetched} game(s)")
    if missed:
        warn(f"Could not download game(s) {missed}; kept any earlier copy and will retry next run.")
    build_data.SRC, build_data.OUT = RAW, "data.json"
    build_data.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
