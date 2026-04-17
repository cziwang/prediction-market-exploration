"""Repository for NBA game results.

Handles DB reads/writes for the games table. Knows the schema,
doesn't know about APIs.
"""

from __future__ import annotations

from app.db.session import get_db

GAMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id     TEXT,
    season_id   TEXT,
    game_date   TEXT,
    team_id     INTEGER,
    team_abbr   TEXT,
    team_name   TEXT,
    matchup     TEXT,
    wl          TEXT,
    pts         INTEGER,
    fgm         INTEGER,
    fga         INTEGER,
    fg_pct      REAL,
    fg3m        INTEGER,
    fg3a        INTEGER,
    fg3_pct     REAL,
    ftm         INTEGER,
    fta         INTEGER,
    ft_pct      REAL,
    oreb        INTEGER,
    dreb        INTEGER,
    reb         INTEGER,
    ast         INTEGER,
    stl         INTEGER,
    blk         INTEGER,
    tov         INTEGER,
    pf          INTEGER,
    plus_minus  REAL,
    PRIMARY KEY (game_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date);
CREATE INDEX IF NOT EXISTS idx_games_team ON games(team_abbr);
"""


def init_games_table() -> None:
    conn = get_db()
    conn.executescript(GAMES_SCHEMA)
    conn.close()


def upsert_games(rows: list[dict]) -> int:
    conn = get_db()
    count = 0
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO games
            (game_id, season_id, game_date, team_id, team_abbr, team_name,
             matchup, wl, pts, fgm, fga, fg_pct, fg3m, fg3a, fg3_pct,
             ftm, fta, ft_pct, oreb, dreb, reb, ast, stl, blk, tov, pf, plus_minus)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["game_id"], r["season_id"], r["game_date"], r["team_id"],
                r["team_abbr"], r["team_name"], r["matchup"], r["wl"],
                r["pts"], r["fgm"], r["fga"], r["fg_pct"],
                r["fg3m"], r["fg3a"], r["fg3_pct"],
                r["ftm"], r["fta"], r["ft_pct"],
                r["oreb"], r["dreb"], r["reb"],
                r["ast"], r["stl"], r["blk"], r["tov"], r["pf"],
                r["plus_minus"],
            ),
        )
        count += 1
    conn.commit()
    conn.close()
    return count
