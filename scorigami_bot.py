"""Michigan State Basketball Scorigami bot.

Checks ESPN for finished MSU men's basketball games. If the final score
(winning score, losing score; order and which team doesn't matter) has never happened before in program history,
it tweets about it. Every finished game is appended to history.csv so the
"already seen" list keeps growing.

Usage:
    python scorigami_bot.py            # check once (dry run unless POST_LIVE=true)
    python scorigami_bot.py --watch    # keep checking every minute while a game is on (~25 min)
    python scorigami_bot.py --stats    # print history summary
Env (from .env locally, or GitHub secrets):
    CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET
    POST_LIVE=true   -> actually tweet. Anything else = print only.
"""
import csv
import json
import math
import os
import sys
import time
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
STATE = HERE / "state.json"
SUMMARY = ("https://site.api.espn.com/apis/site/v2/sports/basketball/"
           "mens-college-basketball/summary")
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


# ---------------------------------------------------------------- live games
def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"live_posted": []}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def parse_clock(display):
    """'12:34' -> 12.57 minutes; '45.3' (seconds only) -> 0.76."""
    display = str(display or "0:00")
    if ":" in display:
        m, sec = display.split(":")
        return int(m) + float(sec) / 60
    return float(display) / 60


def fetch_live_games(year):
    """MSU games that are in progress right now."""
    live = []
    for seasontype in (2, 3):
        r = requests.get(ESPN, params={"season": year, "seasontype": seasontype}, timeout=30)
        r.raise_for_status()
        for e in r.json().get("events", []):
            comp = e["competitions"][0]
            if comp["status"]["type"].get("state") != "in":
                continue
            h = requests.get(SUMMARY, params={"event": e["id"]}, timeout=30)
            h.raise_for_status()
            hc = h.json()["header"]["competitions"][0]
            msu = next(c for c in hc["competitors"] if str(c["team"]["id"]) == "127")
            opp = next(c for c in hc["competitors"] if str(c["team"]["id"]) != "127")
            status = {**hc.get("status", {}), **comp["status"]}
            live.append({
                "espn_id": e["id"],
                "opponent": opp["team"].get("displayName", "Opponent"),
                "msu": int(msu["score"]),
                "opp": int(opp["score"]),
                "period": int(status.get("period", 0)),
                "clock": status.get("displayClock", "0:00"),
            })
    return live


def minutes_left(period, clock):
    """Regulation minutes left, or None in overtime / before the game."""
    if period == 1:
        return 20 + parse_clock(clock)
    if period == 2:
        return parse_clock(clock)
    return None


def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def points_pmf(mean, var, cap=70):
    """P(team scores exactly k more points), k = 0..cap (normal approximation)."""
    if mean <= 0:
        return [1.0] + [0.0] * cap
    sd = math.sqrt(var)
    pmf = []
    for k in range(cap + 1):
        lo = -math.inf if k == 0 else (k - 0.5 - mean) / sd
        hi = (k + 0.5 - mean) / sd
        pmf.append(_cdf(hi) - (0.0 if lo == -math.inf else _cdf(lo)))
    tot = sum(pmf)
    return [x / tot for x in pmf]


def season_rates(rows, years=2):
    """MSU and opponent points per minute over the last few seasons."""
    cutoff = f"{datetime.now(TZ).year - years}-08-01"
    recent = [r for r in rows if r["date"] >= cutoff] or rows[-60:]
    n = len(recent)
    return (sum(int(r["msu"]) for r in recent) / n / 40,
            sum(int(r["opp"]) for r in recent) / n / 40)


def final_score_odds(game, t_left, rows, seen_pairs):
    """Return (P(final is a scorigami), best_unseen_score, its_probability)."""
    elapsed = max(40 - t_left, 0.01)
    prior_m, prior_o = season_rates(rows)
    w = elapsed / (elapsed + 20)          # trust the live pace more as game goes on
    rate_m = w * game["msu"] / elapsed + (1 - w) * prior_m
    rate_o = w * game["opp"] / elapsed + (1 - w) * prior_o
    # points come in 2s and 3s, so variance runs above the mean
    pm = points_pmf(rate_m * t_left, 2.2 * rate_m * t_left)
    po = points_pmf(rate_o * t_left, 2.2 * rate_o * t_left)
    probs = {}
    for k, a in enumerate(pm):
        for l, b in enumerate(po):
            fm, fo = game["msu"] + k, game["opp"] + l
            if fm == fo:                  # a tie means overtime; ignore that sliver
                continue
            key = score_key(fm, fo)
            probs[key] = probs.get(key, 0.0) + a * b
    total = sum(probs.values())
    probs = {k: v / total for k, v in probs.items()}
    unseen = {k: v for k, v in probs.items() if k not in seen_pairs}
    p_new = sum(unseen.values())
    best = max(unseen, key=unseen.get) if unseen else None
    return p_new, best, (unseen[best] if best else 0.0)


def build_live_tweet(game, p_new, best, p_best):
    m, o, period = game["msu"], game["opp"], game["period"]
    if period == 1:
        when = "at halftime"
    else:
        when = f"with {game['clock']} left in the 2nd half"
    text = (
        f"👀 Scorigami watch\n\n"
        f"Michigan State {m}, {game['opponent']} {o} {when}.\n\n"
        f"Chance this ends in a brand-new final score: {p_new * 100:.0f}%\n"
    )
    if best:
        hi, lo = best
        text += f"Most likely scorigami: {hi}-{lo} ({p_best * 100:.1f}%)\n"
    text += "\n#GoGreen"
    return text[:280]


def run_live(rows, seen_pairs, live_flag):
    state = load_state()
    for year in current_season_years():
        for g in fetch_live_games(year):
            if g["espn_id"] in state["live_posted"]:
                continue
            halftime = g["period"] == 1 and parse_clock(g["clock"]) < 0.01
            if not (g["period"] == 2 or halftime):
                continue                  # wait until halftime / 2nd half
            t_left = 20.0 if halftime else minutes_left(g["period"], g["clock"])
            if halftime:
                g = {**g, "period": 1}
            if t_left is None or t_left <= 0:
                continue
            p_new, best, p_best = final_score_odds(g, t_left, rows, seen_pairs)
            text = build_live_tweet(g, p_new, best, p_best)
            print(text)
            state["live_posted"].append(g["espn_id"])
            save_state(state)             # mark first so a crash can't double-post
            if live_flag:
                post(text)
            else:
                print("(dry run: set POST_LIVE=true to tweet)")


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

    try:
        run_live(rows, seen_pairs, live)
    except Exception as e:  # live tweets are a bonus; never break the main job
        print("live-game check failed:", e)


def game_active(year, now=None):
    """True if an MSU game is live or starts within the next 3 hours."""
    now = now or datetime.now(ZoneInfo("UTC"))
    for seasontype in (2, 3):
        r = requests.get(ESPN, params={"season": year, "seasontype": seasontype}, timeout=30)
        r.raise_for_status()
        for e in r.json().get("events", []):
            state = e["competitions"][0]["status"]["type"].get("state")
            if state == "in":
                return True
            if state == "pre":
                start = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                if 0 <= (start - now).total_seconds() <= 3 * 3600:
                    return True
    return False


def watch(minutes=25, every=60):
    """Check every minute for ~25 min; stop early if no game is on or near."""
    end = time.time() + minutes * 60
    while True:
        try:
            active = any(game_active(y) for y in current_season_years())
        except Exception as e:
            print("schedule check failed:", e)
            active = True
        run()                       # always run once AFTER the active check
        if not active or time.time() + every > end:
            break
        time.sleep(every)


if __name__ == "__main__":
    if "--stats" in sys.argv:
        stats()
    elif "--watch" in sys.argv:
        watch()
    else:
        run()
