"""Build the Kalshi event_code -> NBA game_id mapping from PBP data.

Kalshi NBA tickers embed an event code: KXNBAGAME-26APR18ATLNYK-NYK
    26APR18  game date (US/Eastern)
    ATL      away tricode
    NYK      home tricode

NBA game_ids (0042500121) appear nowhere in Kalshi data, so the mapping is
inferred from each game's play-by-play:

- date:      first action's timeActual (UTC) converted to US/Eastern
- home/away: score attribution — when scoreHome increments after an action
             by team T, T is home (majority vote across all scoring actions
             for robustness)

Output: reference/game_map.json  {event_code: game_id}, also uploaded to
s3://prediction-markets-data/reference/game_map.json.

Usage:
    python scripts/build_game_map.py
"""

import gzip
import io
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

BUCKET = "prediction-markets-data"
PBP_PREFIX = "bronze_merged/nba_cdn/"
OUT_LOCAL = Path("reference/game_map.json")
OUT_S3_KEY = "reference/game_map.json"
ET = ZoneInfo("America/New_York")

# Golden game — known-correct anchor; the build fails if it doesn't reproduce
GOLDEN = {"26APR18ATLNYK": "0042500121"}


def infer_game(actions: list[dict]) -> tuple[str, str]:
    """Return (event_code, game_id) for one game's PBP actions."""
    game_id = actions[0]["game_id"]

    home_votes: Counter[str] = Counter()
    away_votes: Counter[str] = Counter()
    prev_home = prev_away = 0
    for rec in actions:
        frame = rec["frame"]
        team = frame.get("teamTricode")
        home, away = int(frame["scoreHome"]), int(frame["scoreAway"])
        if team:
            if home > prev_home and away == prev_away:
                home_votes[team] += 1
            elif away > prev_away and home == prev_home:
                away_votes[team] += 1
        prev_home, prev_away = home, away

    home_team = home_votes.most_common(1)[0][0]
    away_team = away_votes.most_common(1)[0][0]
    if home_team == away_team:
        raise ValueError(f"{game_id}: home/away inference collision ({home_team})")

    first_utc = datetime.fromisoformat(actions[0]["frame"]["timeActual"].replace("Z", "+00:00"))
    et = first_utc.astimezone(ET)
    event_code = f"{et.strftime('%y%b%d').upper()}{away_team}{home_team}"
    return event_code, game_id


def main() -> None:
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix=PBP_PREFIX)
        for obj in page.get("Contents", [])
        if obj["Key"].endswith(".jsonl.gz")
    ]
    print(f"{len(keys)} PBP files")

    game_map: dict[str, str] = {}
    for key in sorted(keys):
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        with gzip.open(io.BytesIO(body), "rt") as f:
            actions = [json.loads(line) for line in f if line.strip()]
        event_code, game_id = infer_game(actions)
        if event_code in game_map:
            raise ValueError(f"duplicate event_code {event_code}: {game_map[event_code]} vs {game_id}")
        game_map[event_code] = game_id
        print(f"  {event_code} -> {game_id}")

    # Anchor validation against the hand-verified golden game
    for code, gid in GOLDEN.items():
        assert game_map.get(code) == gid, f"golden mismatch: {code} -> {game_map.get(code)}"
    print(f"\ngolden anchor OK; {len(game_map)} games mapped")

    OUT_LOCAL.parent.mkdir(exist_ok=True)
    OUT_LOCAL.write_text(json.dumps(game_map, indent=2, sort_keys=True) + "\n")
    s3.put_object(Bucket=BUCKET, Key=OUT_S3_KEY, Body=OUT_LOCAL.read_bytes())
    print(f"wrote {OUT_LOCAL} and s3://{BUCKET}/{OUT_S3_KEY}")


if __name__ == "__main__":
    main()
