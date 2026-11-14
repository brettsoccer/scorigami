# MSU Basketball Scorigami Bot

Tweets when Michigan State men's basketball plays a game whose exact final
score (MSU points - opponent points) has never happened before.

- `history.csv`  every game on record (1949-50 to now) - this is the "already seen" list
- `scorigami_bot.py`  the bot (checks ESPN, appends finished games, tweets new scores)
- `.github/workflows/scorigami.yml`  runs the bot every 30 min, Nov-Apr, on GitHub

Local test (never tweets unless POST_LIVE=true):
    pip install -r requirements.txt
    python scorigami_bot.py --stats
    python scorigami_bot.py

Known gap: Sports-Reference has no game logs before the 1949-50 season, so
earlier games are not in history.csv.
