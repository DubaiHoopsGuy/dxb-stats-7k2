"""Build the tool's data from EuroLeague Media Game Center files.

Reads the season game list and one "bundle" per played game (box score, play-by-play, shots),
and writes data.json with: team season figures (attack and what they allow), league averages,
per-game figures, real game timelines (events and player stints) and player on/off splits.

Method (shown on the tool's Method page):
- Possessions are counted from the play-by-play: a new possession starts whenever the other
  team gains the ball (defensive rebound, or the other team's shot, free throw, turnover or
  offensive rebound), and at the start of each period. Offensive rebounds continue a possession.
- Second-chance points use EuroLeague's official flag on each scoring play.
- Team rebounds and team turnovers are included.
- League averages are pooled from season totals.
"""
import json, os, re, sys, gzip, datetime, collections

SEASON = os.environ.get("SEASON", "E2026")
SRC = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = sys.argv[2] if len(sys.argv) > 2 else "data.json"
PERIODS = ["FirstQuarter", "SecondQuarter", "ThirdQuarter", "ForthQuarter", "ExtraTime"]
SHORT = {"MAD": "Real Madrid", "BAR": "Barcelona", "ULK": "Fenerbahce", "RED": "Crvena Zvezda", "BAS": "Baskonia",
         "MIL": "Milan", "VIR": "Virtus", "OLY": "Olympiacos", "PAN": "Panathinaikos", "HTA": "Hapoel", "PRS": "Paris",
         "ZAL": "Zalgiris", "IST": "Anadolu Efes", "BES": "Besiktas", "MUN": "Bayern", "ASV": "ASVEL", "PAM": "Valencia",
         "TEL": "Maccabi", "DUB": "Dubai", "PAR": "Partizan"}


def nice(name):
    """'BACON, DWAYNE' -> 'Dwayne Bacon'."""
    if not name:
        return None
    if "," in name:
        last, first = [x.strip() for x in name.split(",", 1)]
        name = f"{first} {last}"
    def cap(w):
        if w.upper() in ("II", "III", "IV", "JR.", "JR"):
            return w.upper().replace("JR.", "Jr.").replace("JR", "Jr")
        out = "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))
        if out.startswith("Mc") and len(out) > 2:
            out = "Mc" + out[2].upper() + out[3:]
        if "'" in out:
            out = "'".join(x[:1].upper() + x[1:] for x in out.split("'"))
        return out
    return " ".join(cap(w) for w in name.split())


def clock(period_index, marker):
    """Elapsed game seconds from period (0-based) and the remaining-time marker 'mm:ss'."""
    length = 600 if period_index < 4 else 300
    base = period_index * 600 if period_index < 4 else 2400 + (period_index - 4) * 300
    if not marker:
        return None, base, length
    m, s = marker.split(":")
    return base + length - (int(m) * 60 + int(s)), base, length


def drop_empty(ev, cur):
    """A possession that starts at the buzzer (e.g. a defensive rebound) and ends with the period,
    without a shot, free throw, turnover or rebound, is not counted."""
    if cur and ev[cur] and ev[cur][-1]["type"] == "P":
        ev[cur].pop()


def parse_game(b):
    p = b["pbp"]
    A, B = p["CodeTeamA"].strip(), p["CodeTeamB"].strip()
    other = {A: B, B: A}
    box = {s["PlayersStats"][0]["Team"].strip(): s for s in b["box"]["Stats"]}
    names, minutes, starters, team_of = {}, {}, collections.defaultdict(set), {}
    for code, s in box.items():
        for ps in s["PlayersStats"]:
            pid = ps["Player_ID"].strip()
            names[pid] = nice(ps["Player"])
            team_of[pid] = code
            mm = ps["Minutes"] or "0:00"
            minutes[pid] = int(mm.split(":")[0]) * 60 + int(mm.split(":")[1]) if ":" in mm else 0
            if ps["IsStarter"]:
                starters[code].add(pid)
    scp_plays = {r["NUM_ANOT"] for r in b["points"]["Rows"] if r["SECOND_CHANCE"] == "1"}

    # flatten play-by-play with elapsed time
    raw, end = [], 2400
    for qi, key in enumerate(PERIODS):
        rows = p.get(key) or []
        if key == "ExtraTime" and rows:
            # extra periods are listed together; split on Begin Period markers
            pass
        for e in sorted(rows, key=lambda r: r["NUMBEROFPLAY"]):
            t, base, length = clock(qi, e["MARKERTIME"])
            if key == "ExtraTime":
                end = max(end, base + length)
            raw.append(dict(t=t if t is not None else base, typ=e["PLAYTYPE"], team=(e["CODETEAM"] or "").strip(),
                            pid=(e["PLAYER_ID"] or "").strip(), num=e["NUMBEROFPLAY"], period=qi))

    # on-court tracking for stints
    on = {c: set(starters[c]) for c in (A, B)}
    start = {pid: 0 for c in (A, B) for pid in starters[c]}
    stints = []
    ev = {A: [], B: []}          # each team's attack: events while that team has the ball
    cur, last_period = None, -1
    pending_foul = {}            # team that will shoot -> defender who fouled
    last_miss = {}               # team -> last missed shot type ("2", "3", "FT")
    scp_count = collections.Counter()
    for i, r in enumerate(raw):
        typ, team, pid, t = r["typ"], r["team"], r["pid"], r["t"]
        if r["period"] != last_period:
            drop_empty(ev, cur)
            cur, last_period = None, r["period"]
        if typ == "IN" and team in on and pid:
            on[team].add(pid); start[pid] = t
            continue
        if typ == "OUT" and team in on and pid:
            if pid in on[team]:
                on[team].discard(pid); stints.append([team, pid, start.pop(pid, t), t])
            continue
        if typ == "BP" or not team or team not in other:
            continue
        who = names.get(pid) if pid else None
        if typ in ("2FGM", "2FGA", "3FGM", "3FGA", "FTM", "FTA", "TO", "O"):
            off = team
        elif typ == "D":
            off = other[team]
        else:
            if typ == "CM" and pid:
                pending_foul[other[team]] = who
            if typ == "ST" and pid:
                # attach the steal to the other team's most recent turnover
                for e in reversed(ev[other[team]]):
                    if e["type"] == "TO" and abs(e["t"] - t) <= 3:
                        e["stl"] = who; break
            continue
        if typ == "D":
            ev[off].append({"t": t, "type": "DREB", "by": who})
            if cur != team:
                ev[team].append({"t": t, "type": "P"}); cur = team
            continue
        if off != cur:
            ev[off].append({"t": t, "type": "P"}); cur = off
        e = {"t": t, "by": who}
        if typ in ("2FGM", "2FGA", "3FGM", "3FGA"):
            made = typ.endswith("M"); three = typ[0] == "3"
            e.update(type="FGA", made=made, three=three, pts=(3 if three else 2) if made else 0)
            if not made:
                last_miss[off] = "3" if three else "2"
        elif typ in ("FTM", "FTA"):
            made = typ == "FTM"
            e.update(type="FTA", made=made, pts=1 if made else 0)
            prev = ev[off][-1] if ev[off] else None
            if not (prev and prev["type"] == "FTA" and prev["t"] == t):
                e["trip"] = 1
                if pending_foul.get(off):
                    e["foul"] = pending_foul.pop(off)
            if not made:
                last_miss[off] = "FT"
        elif typ == "TO":
            e.update(type="TO")
        elif typ == "O":
            e.update(type="OREB", after=last_miss.get(off))
        if r["num"] in scp_plays and e.get("pts"):
            e["scp"] = True; scp_count[off] += e["pts"]
        ev[off].append(e)
    drop_empty(ev, cur)
    # attach which missed shot each defensive rebound followed
    for c in (A, B):
        lm = None
        for e in ev[c]:
            if e["type"] == "FGA" and not e["made"]:
                lm = "3" if e["three"] else "2"
            elif e["type"] == "FTA" and not e["made"]:
                lm = "FT"
            elif e["type"] == "DREB":
                e["after"] = lm
    for c in (A, B):
        for pid in list(on[c]):
            stints.append([c, pid, start.pop(pid, end), end])
    st = {A: [], B: []}
    for c, pid, a, z in stints:
        if z > a:
            st[c].append({"p": names.get(pid, pid), "a": a, "b": z})
    pts = {c: int(box[c]["totr"]["Points"]) for c in (A, B)}
    return dict(A=A, B=B, end=end, ev=ev, stints=st, pts=pts, minutes={names[k]: v for k, v in minutes.items() if v},
                team_of={names[k]: v for k, v in team_of.items()}, scp=scp_count)


def counters(events, filt=None):
    c = collections.Counter()
    for e in events:
        if filt and not filt(e):
            continue
        ty = e["type"]
        if ty == "P":
            c["poss"] += 1; continue
        c["pts"] += e.get("pts", 0)
        if e.get("scp"):
            c["scp"] += e.get("pts", 0)
        if ty == "FGA":
            c["fga"] += 1
            if e["three"]:
                c["tpa"] += 1
            else:
                c["twa"] += 1
            if e["made"]:
                c["fgm"] += 1
                if e["three"]:
                    c["tpm"] += 1
                else:
                    c["twm"] += 1
        elif ty == "FTA":
            c["fta"] += 1
            if e["made"]:
                c["ftm"] += 1
        elif ty == "TO":
            c["to"] += 1
            if e.get("stl"):
                c["stl"] += 1
        elif ty == "OREB":
            c["oreb"] += 1
            if e.get("after") in ("2", "3"):
                c["oreb" + e["after"]] += 1
        elif ty == "DREB":
            c["dreb"] += 1
            if e.get("after") in ("2", "3"):
                c["dreb" + e["after"]] += 1
    return c


def kpis(c, secs, games):
    d = lambda a, b: a / b if b else 0
    return {
        "ortg": d(c["pts"], c["poss"]) * 100,
        "pace": d(c["poss"] * 2400, secs),
        "efg": d(c["fgm"] + .5 * c["tpm"], c["fga"]),
        "tov": d(c["to"], c["poss"]),
        "orb": d(c["oreb"], c["oreb"] + c["dreb"]),
        "scp": d(c["scp"], games),
        "ftr": d(c["fta"], c["fga"]),
    }


def components(c, secs, games):
    d = lambda a, b: a / b if b else 0
    return {"pts": d(c["pts"], games), "poss": d(c["poss"], games), "secs": d(secs, 2 * c["poss"]),
            "p2": d(c["twm"], c["twa"]), "p3": d(c["tpm"], c["tpa"]), "r3": d(c["tpa"], c["fga"]),
            "to": d(c["to"], games), "stl": d(c["stl"], games),
            "oreb": d(c["oreb"], games), "orb2": d(c["oreb2"], c["oreb2"] + c["dreb2"]), "orb3": d(c["oreb3"], c["oreb3"] + c["dreb3"]),
            "ppor": d(c["scp"], c["oreb"]), "fta": d(c["fta"], games), "ftp": d(c["ftm"], c["fta"])}


def main():
    games = json.load(open(os.path.join(SRC, "games.json")))["data"]
    played = sorted([g for g in games if g["played"]], key=lambda g: (g["round"], g["utcDate"]))
    upcoming = sorted([g for g in games if not g["played"]], key=lambda g: g["utcDate"])
    names = {}
    for g in games:
        for side in ("local", "road"):
            names[g[side]["club"]["code"]] = g[side]["club"]["name"]
    tot = {c: {"off": collections.Counter(), "def": collections.Counter(), "secs": 0, "n": 0, "w": 0, "l": 0} for c in names}
    gl = {c: [] for c in names}
    players = collections.defaultdict(dict)   # team -> name -> accumulators
    timelines = {}
    flagged = []
    for g in played:
        gc = g["gameCode"]
        fn = os.path.join(SRC, f"b{gc}.json.gz")
        b = json.load(gzip.open(fn, "rt", encoding="utf-8")) if os.path.exists(fn) else json.load(open(os.path.join(SRC, f"b{gc}.json")))
        G = parse_game(b)
        A, B = G["A"], G["B"]
        pa = sum(1 for e in G["ev"][A] if e["type"] == "P"); pb = sum(1 for e in G["ev"][B] if e["type"] == "P")
        if abs(pa - pb) > 2:
            flagged.append({"game": gc, "round": g["round"], "teams": f"{A}-{B}", "poss": [pa, pb]})
        # check points from events agree with the box score
        for c in (A, B):
            ep = sum(e.get("pts", 0) for e in G["ev"][c])
            if ep != G["pts"][c]:
                flagged.append({"game": gc, "round": g["round"], "team": c, "points_events": ep, "points_box": G["pts"][c]})
        home = g["local"]["club"]["code"]
        timelines[gc] = {"end": G["end"], "teams": [A, B], "ev": G["ev"], "stints": G["stints"]}
        for c in (A, B):
            o = other = B if c == A else A
            co, cd = counters(G["ev"][c]), counters(G["ev"][o])
            T = tot[c]; T["off"].update(co); T["def"].update(cd); T["secs"] += G["end"]; T["n"] += 1
            win = G["pts"][c] > G["pts"][o]
            T["w" if win else "l"] += 1
            gl[c].append({"round": g["round"], "gc": gc, "opp": o, "home": c == home, "win": win,
                          "score": f"{G['pts'][c]}–{G['pts'][o]}", "date": g["utcDate"],
                          "off": kpis(co, G["end"], 1), "def": kpis(cd, G["end"], 1)})
            # player on/off: team attack and defense with the player on / off the court
            onset = collections.defaultdict(list)
            for s in G["stints"][c]:
                onset[s["p"]].append((s["a"], s["b"]))
            for pname, spans in onset.items():
                inz = lambda t, spans=spans: any(a <= t < z or (t >= G["end"] and z >= G["end"]) for a, z in spans)
                acc = players[c].setdefault(pname, {"gp": 0, "secs": 0, "team_secs": 0, "on": {"off": collections.Counter(), "def": collections.Counter()}, "offc": {"off": collections.Counter(), "def": collections.Counter()}})
                acc["gp"] += 1; acc["secs"] += sum(z - a for a, z in spans); acc["team_secs"] += G["end"]
                acc["on"]["off"].update(counters(G["ev"][c], lambda e: inz(e["t"])))
                acc["on"]["def"].update(counters(G["ev"][o], lambda e: inz(e["t"])))
                acc["offc"]["off"].update(counters(G["ev"][c], lambda e: not inz(e["t"])))
                acc["offc"]["def"].update(counters(G["ev"][o], lambda e: not inz(e["t"])))
    lg = collections.Counter(); lsecs = 0; ln = 0
    for c, T in tot.items():
        lg.update(T["off"]); lsecs += T["secs"]; ln += T["n"]
    out = {
        "meta": {"season": SEASON, "through_round": played[-1]["round"] if played else 0, "games": len(played),
                 "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                 "method": "possessions counted from play-by-play; official second-chance flag; pooled league averages",
                 "flagged": flagged},
        "league": kpis(lg, lsecs, ln),
        "league_cx": components(lg, lsecs, ln),
        "teams": [], "timelines": timelines,
        "next": [{"gc": g["gameCode"], "round": g["round"], "date": g["utcDate"], "home": g["local"]["club"]["code"], "away": g["road"]["club"]["code"]} for g in upcoming[:40]],
        "schedule": [{"gc": g["gameCode"], "round": g["round"], "date": g["utcDate"], "home": g["local"]["club"]["code"], "away": g["road"]["club"]["code"],
                      "played": bool(g["played"]) and g["gameCode"] in timelines} for g in sorted(games, key=lambda g: g["utcDate"])],
    }
    for c, T in sorted(tot.items()):
        if not T["n"]:
            continue
        pl = []
        for pname, a in sorted(players[c].items(), key=lambda kv: -kv[1]["secs"]):
            pl.append({"name": pname, "gp": a["gp"], "secs": a["secs"], "team_secs": a["team_secs"],
                       "on": {s: dict(a["on"][s]) for s in ("off", "def")}, "offc": {s: dict(a["offc"][s]) for s in ("off", "def")}})
        out["teams"].append({"code": c, "name": SHORT.get(c, names[c]), "full": names[c], "w": T["w"], "l": T["l"],
                             "off": kpis(T["off"], T["secs"], T["n"]), "def": kpis(T["def"], T["secs"], T["n"]),
                             "cx": {"off": components(T["off"], T["secs"], T["n"]), "def": components(T["def"], T["secs"], T["n"])},
                             "games": sorted(gl[c], key=lambda x: x["round"]), "players": pl})
    json.dump(out, open(OUT, "w"), separators=(",", ":"))
    print(f"{len(played)} games through round {out['meta']['through_round']}; {len(flagged)} flagged; {os.path.getsize(OUT)//1024} KB")
    for f in flagged:
        print("  flagged:", f)


if __name__ == "__main__":
    main()
