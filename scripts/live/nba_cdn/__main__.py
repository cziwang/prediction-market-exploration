"""Live NBA CDN ingester → bronze S3.

Polls cdn.nba.com for the scoreboard, odds, per-game play-by-play, and
per-game boxscore. Each response is emitted to bronze via BronzeWriter.
No transform, no silver — just raw frames archived to S3.

Architecture: four independent polling loops run concurrently:
  - scoreboard_loop: discovers games, updates shared active/final sets
  - odds_loop: polls odds independently
  - pbp_loop: polls PBP for all active games concurrently
  - boxscore_loop: polls boxscores for all active games concurrently

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
from dataclasses import dataclass, field

import httpx

from app.clients.nba_cdn import (
    BOXSCORE_URL,
    HEADERS,
    ODDS_URL,
    PBP_URL,
    SCOREBOARD_URL,
)
from app.services.bronze_writer import BronzeWriter

# Poll intervals per channel (seconds)
PBP_INTERVAL = 1.0
BOXSCORE_INTERVAL = 5.0
SCOREBOARD_INTERVAL_LIVE = 10.0
SCOREBOARD_INTERVAL_ACTIVE = 60.0
SCOREBOARD_INTERVAL_IDLE = 300.0
ODDS_INTERVAL = 10.0

log = logging.getLogger("live.nba_cdn")


@dataclass
class GameState:
    game_id: str
    last_action_number: int = 0
    poll_seq: int = 0
    pbp_etag: str | None = None
    boxscore_etag: str | None = None


@dataclass
class Stats:
    """Request counters for rate-limit monitoring."""
    requests: int = 0
    responses_304: int = 0
    errors: int = 0
    started: float = field(default_factory=time.time)

    def rps(self) -> float:
        elapsed = time.time() - self.started
        return self.requests / elapsed if elapsed > 0 else 0.0


class Ingester:
    def __init__(self, bronze: BronzeWriter) -> None:
        self.bronze = bronze
        self.games: dict[str, GameState] = {}
        self.finalized: set[str] = set()
        self.active_ids: set[str] = set()
        self.has_slate: bool = False
        self.odds_etag: str | None = None
        self._shutdown = asyncio.Event()
        self.stats = Stats()
        self._final_gs: dict[str, GameState] = {}
        self._final_pbp_done: set[str] = set()
        self._final_box_done: set[str] = set()

    async def run(self) -> None:
        async with httpx.AsyncClient(headers=HEADERS, timeout=5.0) as client:
            self.client = client
            tasks = [
                asyncio.create_task(self._scoreboard_loop(), name="scoreboard"),
                asyncio.create_task(self._odds_loop(), name="odds"),
                asyncio.create_task(self._pbp_loop(), name="pbp"),
                asyncio.create_task(self._boxscore_loop(), name="boxscore"),
                asyncio.create_task(self._stats_loop(), name="stats"),
            ]
            await self._shutdown.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- Scoreboard loop: game discovery ------------------------------------

    async def _scoreboard_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                interval = await self._poll_scoreboard()
            except Exception:
                log.exception("scoreboard poll failed")
                interval = SCOREBOARD_INTERVAL_ACTIVE
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

    async def _poll_scoreboard(self) -> float:
        t_request = time.time()
        self.stats.requests += 1
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

        self.has_slate = bool(active or final or scheduled)

        if self.has_slate:
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

        # Track new active games
        for gid in active:
            if gid not in self.games and gid not in self.finalized:
                self.games[gid] = GameState(game_id=gid)
                log.info("tracking live game %s", gid)

        # Finalize games
        for gid in final:
            if gid in self.games and gid not in self.finalized:
                self.finalized.add(gid)
                self.games.pop(gid, None)
                log.info("game %s final — will get one last pbp+boxscore poll", gid)

        self.active_ids = active

        if active:
            return SCOREBOARD_INTERVAL_LIVE
        if scheduled or final:
            return SCOREBOARD_INTERVAL_ACTIVE
        return SCOREBOARD_INTERVAL_IDLE

    # -- Odds loop ----------------------------------------------------------

    async def _odds_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                if self.has_slate:
                    await self._poll_odds()
            except Exception:
                log.exception("odds poll failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=ODDS_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass

    async def _poll_odds(self) -> None:
        headers = {"If-None-Match": self.odds_etag} if self.odds_etag else {}
        t_request = time.time()
        self.stats.requests += 1
        resp = await self.client.get(ODDS_URL, headers=headers)
        t_receipt = time.time()
        if resp.status_code == 304:
            self.stats.responses_304 += 1
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

    # -- PBP loop: all active games concurrently ----------------------------

    async def _pbp_loop(self) -> None:
        while not self._shutdown.is_set():
            game_ids = list(self.active_ids)
            # Also do one final poll for newly finalized games
            final_games = [gid for gid in self.finalized if gid not in self._final_pbp_done]
            all_ids = game_ids + final_games

            if all_ids:
                tasks = []
                for gid in all_ids:
                    gs = self.games.get(gid) or self._get_or_create_final_gs(gid)
                    tasks.append(self._poll_pbp(gs))
                await asyncio.gather(*tasks, return_exceptions=True)
                for gid in final_games:
                    self._final_pbp_done.add(gid)

            interval = PBP_INTERVAL if self.active_ids else SCOREBOARD_INTERVAL_ACTIVE
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

    async def _poll_pbp(self, gs: GameState) -> None:
        url = PBP_URL.format(gs.game_id)
        headers = {"If-None-Match": gs.pbp_etag} if gs.pbp_etag else {}
        t_request = time.time()
        self.stats.requests += 1
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("pbp fetch failed for %s: %s", gs.game_id, e)
            self.stats.errors += 1
            return
        t_receipt = time.time()
        if resp.status_code == 304:
            self.stats.responses_304 += 1
            return
        if resp.status_code == 429:
            log.warning("pbp 429 for %s — backing off", gs.game_id)
            self.stats.errors += 1
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

    # -- Boxscore loop: all active games concurrently -----------------------

    async def _boxscore_loop(self) -> None:
        while not self._shutdown.is_set():
            game_ids = list(self.active_ids)
            final_games = [gid for gid in self.finalized if gid not in self._final_box_done]
            all_ids = game_ids + final_games

            if all_ids:
                tasks = []
                for gid in all_ids:
                    gs = self.games.get(gid) or self._get_or_create_final_gs(gid)
                    tasks.append(self._poll_boxscore(gs))
                await asyncio.gather(*tasks, return_exceptions=True)
                for gid in final_games:
                    self._final_box_done.add(gid)

            interval = BOXSCORE_INTERVAL if self.active_ids else SCOREBOARD_INTERVAL_ACTIVE
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

    async def _poll_boxscore(self, gs: GameState) -> None:
        url = BOXSCORE_URL.format(gs.game_id)
        headers = {"If-None-Match": gs.boxscore_etag} if gs.boxscore_etag else {}
        t_request = time.time()
        self.stats.requests += 1
        try:
            resp = await self.client.get(url, headers=headers)
        except httpx.HTTPError as e:
            log.warning("boxscore fetch failed for %s: %s", gs.game_id, e)
            self.stats.errors += 1
            return
        t_receipt = time.time()
        if resp.status_code == 304:
            self.stats.responses_304 += 1
            return
        if resp.status_code == 429:
            log.warning("boxscore 429 for %s — backing off", gs.game_id)
            self.stats.errors += 1
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

    # -- Stats loop: periodic RPS logging -----------------------------------

    async def _stats_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                s = self.stats
                log.info(
                    "stats: %d reqs (%.1f rps), %d 304s, %d errors, %d active games",
                    s.requests, s.rps(), s.responses_304, s.errors,
                    len(self.active_ids),
                )

    # -- Helpers ------------------------------------------------------------

    def _get_or_create_final_gs(self, game_id: str) -> GameState:
        if game_id not in self._final_gs:
            self._final_gs[game_id] = GameState(game_id=game_id)
        return self._final_gs[game_id]

    def shutdown(self) -> None:
        log.info("shutdown requested")
        self._shutdown.set()


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
