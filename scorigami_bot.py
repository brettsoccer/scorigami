"""Michigan State Basketball Scorigami bot.

Checks ESPN for finished MSU men's basketball games. If the final score
(winning score, losing score; order and which team doesn't matter) has never happened before in program history,
it tweets about it. Every finished game is appended to history.csv so the
"already seen" list keeps growing.

Usage:
    python scorigami_bot.py            # check once (dry run unless POST_LIVE=true)
    python scorigami_bot.py --stats    # print history summary
Env (from .env locally, or GitHub secrets):
    CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET
    POST_LIVE=true   -> actually tweet. Anything else = print only.
"""
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

HERE = Path(__file__).parent
HISTORY = HERE / "history.csv"
ESPN = ("https://site.api.espn.com/apis/site/v2/sports/basketball/"
        "mens-college-basketball/teams/127/schedule")
TZ = ZoneInfo("America/New_York")
FIELDS = ["date", "opponent", "msu", "opp", "espn_id"]


def load_history():
    if not HISTORY.exists():
        sys.exit("history.csv not found")
    with open(HISTORY, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_history(row):
    with open(HISTORY, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)


def _score(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    return int(float(s))


def current_season_years(now=None):
    now = now or datetime.now(TZ)
    # ESPN labels a season by the year it ends (2026-27 -> 2027)
    end = now.year + 1 if now.month >= 8 else now.year
    return [end]


def fetch_finished_games(year):
    games = []
    for seasontype in (2, 3):  # regular season, postseason
        r = requests.get(ESPN, params={"season": year, "seasontype": seasontype}, timeout=30)
        r.raise_for_status()
        for e in r.json().get("events", []):
            comp = e["competitions"][0]
            if not comp["status"]["type"]["completed"]:
                continue
            msu = next(c for c in comp["competitors"] if c["team"]["id"] == "127")
            opp = next(c for c in comp["competitors"] if c["team"]["id"] != "127")
            when = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(TZ)
            games.append({
                "date": when.strftime("%Y-%m-%d"),
                "opponent": opp["team"].get("displayName", "Opponent"),
                "msu": _score(msu),
                "opp": _score(opp),
                "espn_id": e["id"],
            })
    return sorted(games, key=lambda g: g["date"])


def score_key(a, b):
    """Order doesn't matter: MSU 79-73 and a 73-79 loss are the same score."""
    return (max(a, b), min(a, b))


def ordinal(n):
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def pretty_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%b ") + str(d.day) + d.strftime(", %Y")


def previous_matches(rows, key):
    return [r for r in rows if score_key(int(r["msu"]), int(r["opp"])) == key]


def build_tweet(game, rows, unique_count, total_games):
    """Post-game tweet. `rows` = history BEFORE this game was added."""
    m, o = game["msu"], game["opp"]
    hi, lo = score_key(m, o)
    result = "defeated" if m > o else "fell to"
    matches = previous_matches(rows, (hi, lo))
    a = "An" if str(hi).startswith("8") or hi in (11, 18) else "A"
    if not matches:
        text = (
            f"🚨 SCORIGAMI! 🚨\n\n"
            f"Michigan State {result} {game['opponent']} {m}-{o}.\n\n"
            f"{a} {hi}-{lo} final has never happened in Spartan basketball history (either way).\n\n"
            f"Score #{unique_count:,} out of {total_games:,} games. #GoGreen"
        )
    else:
        last = max(matches, key=lambda r: r["date"])
        lm, lo_pts = int(last["msu"]), int(last["opp"])
        wl = "W" if lm > lo_pts else "L"
        n = len(matches)
        times = "1 time" if n == 1 else f"{n} times"
        text = (
            f"Final: Michigan State {m}, {game['opponent']} {o}.\n\n"
            f"No scorigami. {a} {hi}-{lo} final has happened {times} before, "
            f"most recently on {pretty_date(last['date'])} "
            f"({wl} {lm}-{lo_pts} vs {last['opponent']}).\n\n#GoGreen"
        )
    if len(text) > 280:  # safety: drop the hashtag, then trim
        text = text.replace("\n\n#GoGreen", "")[:280]
    return text


def post(text):
    import tweepy
    client = tweepy.Client(
        consumer_key=os.environ["CONSUMER_KEY"],
        consumer_secret=os.environ["CONSUMER_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text)
    print("Posted tweet id", resp.data["id"])


def stats():
    rows = load_history()
    pairs = {score_key(int(r["msu"]), int(r["opp"])) for r in rows}
    print(f"{len(rows)} games, {len(pairs)} unique final scores")
    print(f"first game: {rows[0]['date']}   latest: {rows[-1]['date']}")


def run():
    rows = load_history()
    seen_pairs = {score_key(int(r["msu"]), int(r["opp"])) for r in rows}
    seen_ids = {r["espn_id"] for r in rows if r["espn_id"]}
    # Historical rows have no ESPN id, so a game is also recognised by date + score.
    seen_keys = {(r["date"], int(r["msu"]), int(r["opp"])) for r in rows}
    live = os.getenv("POST_LIVE", "").lower() == "true"

    for year in current_season_years():
        for g in fetch_finished_games(year):
            if g["espn_id"] in seen_ids or (g["date"], g["msu"], g["opp"]) in seen_keys:
                continue
            pair = score_key(g["msu"], g["opp"])
            is_new = pair not in seen_pairs
            before = list(rows)  # history without this game
            # Record the game FIRST so a crash can never cause a duplicate tweet.
            append_history(g)
            rows.append({k: str(v) for k, v in g.items()})
            seen_ids.add(g["espn_id"])
            seen_keys.add((g["date"], g["msu"], g["opp"]))
            seen_pairs.add(pair)
            print(f"{g['date']}  MSU {g['msu']} - {g['opp']} {g['opponent']}  ->",
                  "SCORIGAMI" if is_new else "seen before")
            text = build_tweet(g, before, len(seen_pairs), len(rows))
            print(text)
            if live:
                try:
                    post(text)
                except Exception as e:  # keep going; game is already recorded
                    print("TWEET FAILED:", e)
                    raise
            else:
                print("(dry run: set POST_LIVE=true to tweet)")


if __name__ == "__main__":
    stats() if "--stats" in sys.argv else run()
