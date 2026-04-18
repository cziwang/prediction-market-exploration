"""Live play-by-play poller for NBA games.

Runs continuously. Discovers active games via the scoreboard, polls PBP
every few seconds, and appends timestamped actions to local JSONL files.
When a game goes final, gzips the file and uploads to S3.

Usage:
    python -m scripts.nba_cdn.poll_live
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import signal
import time
from pathlib import Path

import httpx

from app.core.config import S3_BUCKET

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://cdn.nba.com/static/json/liveData"
SCOREBOARD_URL = f"{BASE_URL}/scoreboard/todaysScoreboard_00.json"
PBP_URL = f"{BASE_URL}/playbyplay/playbyplay_{{}}.json"

DATA_DIR = Path("data/live_pbp")

POLL_INTERVAL = 3.0  # seconds between PBP polls
SCOREBOARD_INTERVAL_IDLE = 300.0  # 5 min when no games on schedule
SCOREBOARD_INTERVAL_ACTIVE = 60.0  # 1 min when games scheduled or in progress
SNAPSHOT_EVERY = 20  # store a snapshot every N polls per game

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

log = logging.getLogger("poll_live")

# ---------------------------------------------------------------------------
# Per-game state
# ---------------------------------------------------------------------------


class GameState:
    """Tracks polling state for a single game."""

    def __init__(self, game_id: str, last_action_number: int = 0, poll_seq: int = 0):
        self.game_id = game_id
        self.last_action_number = last_action_number
        self.poll_seq = poll_seq
        self.etag: str | None = None
        self.file_path = DATA_DIR / f"{game_id}.jsonl"

    def append(self, lines: list[dict]) -> None:
        with open(self.file_path, "a") as f:
            for line in lines:
                f.write(json.dumps(line, default=str) + "\n")

    @classmethod
    def resume(cls, game_id: str) -> GameState:
        """Resume from an existing JSONL file if present."""
        path = DATA_DIR / f"{game_id}.jsonl"
        last_action = 0
        poll_seq = 0
        if path.exists():
            with open(path) as f:
                for raw in f:
                    row = json.loads(raw)
                    if row.get("snapshot"):
                        poll_seq = row.get("poll_seq", poll_seq)
                    else:
                        last_action = max(last_action, row.get("action_number", 0))
                        poll_seq = max(poll_seq, row.get("poll_seq", 0))
            log.info(
                "Resuming %s from action_number=%d poll_seq=%d",
                game_id,
                last_action,
                poll_seq,
            )
        return cls(game_id, last_action_number=last_action, poll_seq=poll_seq)


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------


def upload_to_s3(game_id: str, local_path: Path) -> str:
    """Gzip and upload a completed game file to S3."""
    import boto3

    s3_key = f"nba_cdn/live_pbp/{game_id}.jsonl.gz"
    compressed = gzip.compress(local_path.read_bytes())

    boto3.client("s3").put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=compressed,
        ContentType="application/gzip",
    )
    return s3_key


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class Poller:
    def __init__(self) -> None:
        self.games: dict[str, GameState] = {}
        self.finalized: set[str] = set()  # game IDs already uploaded
        self._shutdown = False

    async def run(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Resume any in-progress games from local files
        for f in DATA_DIR.glob("*.jsonl"):
            game_id = f.stem
            self.games[game_id] = GameState.resume(game_id)
            log.info("Found in-progress file for %s", game_id)

        async with httpx.AsyncClient(headers=HEADERS, timeout=5.0) as client:
            self.client = client
            while not self._shutdown:
                try:
                    await self._tick()
                except Exception:
                    log.exception("Tick failed")
                    await asyncio.sleep(SCOREBOARD_INTERVAL_ACTIVE)

    async def _tick(self) -> None:
        """One full cycle: refresh scoreboard, poll all active games, sleep."""
        scoreboard = await self._fetch_scoreboard()
        games_by_status = self._parse_scoreboard(scoreboard)

        active_ids = games_by_status.get(2, set())
        final_ids = games_by_status.get(3, set())
        scheduled_ids = games_by_status.get(1, set())

        # Start tracking new active games
        for gid in active_ids:
            if gid not in self.games and gid not in self.finalized:
                self.games[gid] = GameState.resume(gid)
                log.info("Game %s is now live — starting poller", gid)

        # Poll PBP for all active games (round-robin)
        if active_ids:
            for gid in list(self.games):
                if gid in active_ids and not self._shutdown:
                    await self._poll_game(self.games[gid])

        # Finalize games that went final
        for gid in final_ids:
            if gid in self.games and gid not in self.finalized:
                await self._finalize_game(self.games[gid])

        # Decide how long to sleep
        if active_ids:
            await asyncio.sleep(POLL_INTERVAL)
        elif scheduled_ids or final_ids:
            await asyncio.sleep(SCOREBOARD_INTERVAL_ACTIVE)
        else:
            await asyncio.sleep(SCOREBOARD_INTERVAL_IDLE)

    async def _fetch_scoreboard(self) -> dict:
        resp = await self.client.get(SCOREBOARD_URL)
        resp.raise_for_status()
        return resp.json()

    def _parse_scoreboard(self, data: dict) -> dict[int, set[str]]:
        """Group game IDs by gameStatus (1=scheduled, 2=live, 3=final)."""
        result: dict[int, set[str]] = {}
        for g in data.get("scoreboard", {}).get("games", []):
            status = g.get("gameStatus", 0)
            result.setdefault(status, set()).add(g["gameId"])
        return result

    async def _poll_game(self, gs: GameState) -> None:
        """Fetch PBP for a single game, append new actions."""
        url = PBP_URL.format(gs.game_id)
        headers = {}
        if gs.etag:
            headers["If-None-Match"] = gs.etag

        t_request = time.time()
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("PBP fetch failed for %s: %s", gs.game_id, e)
            return

        t_receipt = time.time()

        if resp.status_code == 304:
            return  # no change
        resp.raise_for_status()

        gs.etag = resp.headers.get("etag")
        data = resp.json()
        actions = data.get("game", {}).get("actions", [])

        new_actions = [a for a in actions if a["actionNumber"] > gs.last_action_number]
        if not new_actions:
            return

        gs.poll_seq += 1
        lines: list[dict] = []
        for a in new_actions:
            lines.append(
                {
                    "game_id": gs.game_id,
                    "action_number": a["actionNumber"],
                    "t_request": t_request,
                    "t_receipt": t_receipt,
                    "poll_seq": gs.poll_seq,
                    "action": a,
                }
            )
        gs.last_action_number = new_actions[-1]["actionNumber"]

        # Periodic snapshot for correction detection
        if gs.poll_seq % SNAPSHOT_EVERY == 0:
            all_actions_json = json.dumps(actions, sort_keys=True)
            actions_hash = hashlib.sha256(all_actions_json.encode()).hexdigest()
            last_action = actions[-1] if actions else {}
            lines.append(
                {
                    "game_id": gs.game_id,
                    "snapshot": True,
                    "t_receipt": t_receipt,
                    "poll_seq": gs.poll_seq,
                    "total_actions": len(actions),
                    "actions_hash": f"sha256:{actions_hash}",
                    "score": {
                        "home": last_action.get("scoreHome"),
                        "away": last_action.get("scoreAway"),
                    },
                    "period": last_action.get("period"),
                    "clock": last_action.get("clock"),
                    "game_status": data.get("game", {}).get("gameStatus"),
                }
            )

        gs.append(lines)
        log.info(
            "%s: +%d actions (total %d, poll #%d)",
            gs.game_id,
            len(new_actions),
            gs.last_action_number,
            gs.poll_seq,
        )

    async def _finalize_game(self, gs: GameState) -> None:
        """Do a final PBP poll, upload to S3, clean up."""
        log.info("Finalizing %s — last PBP poll", gs.game_id)
        await self._poll_game(gs)

        if gs.file_path.exists():
            try:
                s3_key = upload_to_s3(gs.game_id, gs.file_path)
                log.info("Uploaded %s -> s3://%s/%s", gs.game_id, S3_BUCKET, s3_key)
                gs.file_path.unlink()
            except Exception:
                log.exception("S3 upload failed for %s — keeping local file", gs.game_id)
                return

        self.finalized.add(gs.game_id)
        self.games.pop(gs.game_id, None)

    def shutdown(self) -> None:
        log.info("Shutdown requested — finishing current cycle")
        self._shutdown = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    poller = Poller()

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, poller.shutdown)

    try:
        loop.run_until_complete(poller.run())
    finally:
        loop.close()
        log.info("Poller stopped")


if __name__ == "__main__":
    main()
