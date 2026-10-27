"""Download new EuroLeague games and rebuild data.json.

Run by GitHub every 20 minutes (see .github/workflows/update.yml), or by hand.
- Fetches the season's game list.
- Downloads each finished game once and keeps it in raw/ (compressed), so nothing is
  re-downloaded from scratch.
- Re-downloads games finished in the last 2 days, to pick up EuroLeague's corrections.
- Rebuilds data.json with build_data.py.
"""
import json, os, gzip, time, datetime, urllib.request
import build_data

SEASON = os.environ.get("SEASON", "E2026")
API = "https://statssupercup.euroleague.net/api"
RAW = "raw"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dubai-team-stats/1.0", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data)


def main():
    os.makedirs(RAW, exist_ok=True)
    games = get(f"{API}/games?seasonCode={SEASON}")
    json.dump(games, open(os.path.join(RAW, "games.json"), "w"))
    now = datetime.datetime.now(datetime.timezone.utc)
    fetched = 0
    for g in games["data"]:
        if not g.get("played"):
            continue
        fn = os.path.join(RAW, f"b{g['gameCode']}.json.gz")
        when = datetime.datetime.fromisoformat(g["utcDate"].replace("Z", "+00:00"))
        recent = (now - when).total_seconds() < 2 * 86400
        if os.path.exists(fn) and not recent:
            continue
        b = get(f"{API}/bundle?gamecode={g['gameCode']}&seasoncode={SEASON}")
        if not (b.get("pbp") and b.get("box")):
            continue
        with gzip.open(fn, "wt", encoding="utf-8") as f:
            json.dump(b, f, separators=(",", ":"))
        fetched += 1
        time.sleep(0.5)  # be polite to EuroLeague's servers
    print(f"downloaded {fetched} game(s)")
    build_data.SRC, build_data.OUT = RAW, "data.json"
    build_data.main()


if __name__ == "__main__":
    main()
