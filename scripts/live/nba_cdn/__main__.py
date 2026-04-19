"""Live NBA CDN ingester → bronze S3.

Polls cdn.nba.com for the scoreboard, odds, per-game play-by-play, and
per-game boxscore. Each response is emitted to bronze via BronzeWriter.
No transform, no silver — just raw frames archived to S3.

Channels:
  - scoreboard: one record per scoreboard poll (full response)
  - odds:       one record per odds change (etag-deduped)
  - live_pbp:   one record per new action across all live games
  - boxscore:   one record per boxscore change per live game (etag-deduped)

Usage:
    python -m scripts.live.nba_cdn
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass

import httpx

from app.clients.nba_cdn import (
    BOXSCORE_URL,
    HEADERS,
    ODDS_URL,
    PBP_URL,
    SCOREBOARD_URL,
)
from app.services.bronze_writer import BronzeWriter

POLL_INTERVAL = 3.0
SCOREBOARD_INTERVAL_IDLE = 300.0
SCOREBOARD_INTERVAL_ACTIVE = 60.0

log = logging.getLogger("live.nba_cdn")


@dataclass
class GameState:
    game_id: str
    last_action_number: int = 0
    poll_seq: int = 0
    pbp_etag: str | None = None
    boxscore_etag: str | None = None


class Ingester:
    def __init__(self, bronze: BronzeWriter) -> None:
        self.bronze = bronze
        self.games: dict[str, GameState] = {}
        self.finalized: set[str] = set()
        self.odds_etag: str | None = None
        self._shutdown = False

    async def run(self) -> None:
        async with httpx.AsyncClient(headers=HEADERS, timeout=5.0) as client:
            self.client = client
            while not self._shutdown:
                try:
                    sleep_s = await self._tick()
                except Exception:
                    log.exception("tick failed")
                    sleep_s = SCOREBOARD_INTERVAL_ACTIVE
                if self._shutdown:
                    break
                await asyncio.sleep(sleep_s)

    async def _tick(self) -> float:
        t_request = time.time()
        resp = await self.client.get(SCOREBOARD_URL)
        resp.raise_for_status()
        t_receipt = time.time()
        data = resp.json()

        by_status: dict[int, set[str]] = {}
        for g in data.get("scoreboard", {}).get("games", []):
            by_status.setdefault(g.get("gameStatus", 0), set()).add(g["gameId"])

        active = by_status.get(2, set())
        final = by_status.get(3, set())
        scheduled = by_status.get(1, set())

        # Only archive scoreboard + odds when the slate has anything on it.
        # An empty slate (off-season / off-day) produces an identical empty
        # games array every poll; writing that every 5 min clutters S3 and
        # offers nothing to query later.
        if active or final or scheduled:
            await self.bronze.emit(
                {
                    "source": "nba_cdn",
                    "channel": "scoreboard",
                    "t_request": t_request,
                    "t_receipt": t_receipt,
                    "frame": data,
                },
                channel="scoreboard",
            )
            await self._poll_odds()

        for gid in active:
            if gid not in self.games and gid not in self.finalized:
                self.games[gid] = GameState(game_id=gid)
                log.info("tracking live game %s", gid)

        for gid in list(self.games):
            if gid in active and not self._shutdown:
                await self._poll_game(self.games[gid])
                await self._poll_boxscore(self.games[gid])

        for gid in final:
            if gid in self.games and gid not in self.finalized:
                await self._poll_game(self.games[gid])
                await self._poll_boxscore(self.games[gid])
                self.finalized.add(gid)
                self.games.pop(gid, None)
                log.info("game %s final", gid)

        if active:
            return POLL_INTERVAL
        if scheduled or final:
            return SCOREBOARD_INTERVAL_ACTIVE
        return SCOREBOARD_INTERVAL_IDLE

    async def _poll_game(self, gs: GameState) -> None:
        url = PBP_URL.format(gs.game_id)
        headers = {"If-None-Match": gs.pbp_etag} if gs.pbp_etag else {}
        t_request = time.time()
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("pbp fetch failed for %s: %s", gs.game_id, e)
            return
        t_receipt = time.time()
        if resp.status_code == 304:
            return
        resp.raise_for_status()

        gs.pbp_etag = resp.headers.get("etag")
        data = resp.json()
        actions = data.get("game", {}).get("actions", [])
        new_actions = [a for a in actions if a["actionNumber"] > gs.last_action_number]
        if not new_actions:
            return

        gs.poll_seq += 1
        for a in new_actions:
            await self.bronze.emit(
                {
                    "source": "nba_cdn",
                    "channel": "live_pbp",
                    "game_id": gs.game_id,
                    "action_number": a["actionNumber"],
                    "t_request": t_request,
                    "t_receipt": t_receipt,
                    "poll_seq": gs.poll_seq,
                    "frame": a,
                },
                channel="live_pbp",
            )
        gs.last_action_number = new_actions[-1]["actionNumber"]
        log.info(
            "%s: +%d actions (up to #%d, poll %d)",
            gs.game_id,
            len(new_actions),
            gs.last_action_number,
            gs.poll_seq,
        )

    async def _poll_odds(self) -> None:
        headers = {"If-None-Match": self.odds_etag} if self.odds_etag else {}
        t_request = time.time()
        try:
            resp = await self.client.get(ODDS_URL, headers=headers)
        except httpx.HTTPError as e:
            log.warning("odds fetch failed: %s", e)
            return
        t_receipt = time.time()
        if resp.status_code == 304:
            return
        resp.raise_for_status()

        self.odds_etag = resp.headers.get("etag")
        await self.bronze.emit(
            {
                "source": "nba_cdn",
                "channel": "odds",
                "t_request": t_request,
                "t_receipt": t_receipt,
                "frame": resp.json(),
            },
            channel="odds",
        )

    async def _poll_boxscore(self, gs: GameState) -> None:
        url = BOXSCORE_URL.format(gs.game_id)
        headers = {"If-None-Match": gs.boxscore_etag} if gs.boxscore_etag else {}
        t_request = time.time()
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("boxscore fetch failed for %s: %s", gs.game_id, e)
            return
        t_receipt = time.time()
        if resp.status_code == 304:
            return
        resp.raise_for_status()

        gs.boxscore_etag = resp.headers.get("etag")
        await self.bronze.emit(
            {
                "source": "nba_cdn",
                "channel": "boxscore",
                "game_id": gs.game_id,
                "t_request": t_request,
                "t_receipt": t_receipt,
                "frame": resp.json(),
            },
            channel="boxscore",
        )

    def shutdown(self) -> None:
        log.info("shutdown requested")
        self._shutdown = True


async def _main() -> None:
    async with BronzeWriter(source="nba_cdn") as bronze:
        ingester = Ingester(bronze)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, ingester.shutdown)
        await ingester.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())


if __name__ == "__main__":
    main()
