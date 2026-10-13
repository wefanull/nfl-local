import sys
from pathlib import Path

import duckdb
import nflreadpy as nfl

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB = BASE / "nfl.duckdb"

TEAM_GAMES_SQL = """
CREATE OR REPLACE TABLE team_games AS
SELECT
    t.season,
    t.week,
    t.season_type,                       -- REG = temporada regular, POST = playoffs
    s.gameday,
    t.team,
    t.opponent_team                                             AS opponent,
    (t.team = s.home_team)                                      AS is_home,
    CASE WHEN t.team = s.home_team THEN s.home_score ELSE s.away_score END AS points_for,
    CASE WHEN t.team = s.home_team THEN s.away_score ELSE s.home_score END AS points_against,
    t.passing_yards                                             AS pass_yards,
    t.rushing_yards                                             AS rush_yards,
    t.passing_yards + t.rushing_yards                           AS total_yards,
    t.passing_epa                                               AS pass_epa,
    t.rushing_epa                                               AS rush_epa,
    t.passing_tds + t.rushing_tds                               AS offensive_tds,
    t.passing_interceptions                                     AS interceptions_thrown,
    COALESCE(t.rushing_fumbles_lost, 0) + COALESCE(t.receiving_fumbles_lost, 0)
        + COALESCE(t.sack_fumbles_lost, 0)                      AS fumbles_lost,
    t.sacks_suffered                                            AS sacks_taken,
    t.def_sacks                                                 AS sacks_made,
    t.def_interceptions                                         AS interceptions_made,
    t.penalties,
    t.penalty_yards,
    -- Líneas desde la perspectiva del equipo (convención de apuestas: -3.5 = favorito por 3.5)
    CASE WHEN t.team = s.home_team THEN -s.spread_line ELSE s.spread_line END AS team_spread,
    CASE
        WHEN s.spread_line IS NULL THEN NULL
        WHEN (CASE WHEN t.team = s.home_team THEN s.home_score - s.away_score
                   ELSE s.away_score - s.home_score END)
             + (CASE WHEN t.team = s.home_team THEN -s.spread_line ELSE s.spread_line END) > 0 THEN 'COVER'
        WHEN (CASE WHEN t.team = s.home_team THEN s.home_score - s.away_score
                   ELSE s.away_score - s.home_score END)
             + (CASE WHEN t.team = s.home_team THEN -s.spread_line ELSE s.spread_line END) < 0 THEN 'NO_COVER'
        ELSE 'PUSH'
    END                                                         AS ats_result,
    s.total_line,
    CASE
        WHEN s.total_line IS NULL THEN NULL
        WHEN s.home_score + s.away_score > s.total_line THEN 'OVER'
        WHEN s.home_score + s.away_score < s.total_line THEN 'UNDER'
        ELSE 'PUSH'
    END                                                         AS ou_result
FROM team_stats t
JOIN schedules s ON s.game_id = t.game_id
WHERE s.home_score IS NOT NULL AND s.away_score IS NOT NULL   -- solo juegos ya jugados
"""


GAMES_SQL = """
CREATE OR REPLACE TABLE games AS
WITH base AS (
    SELECT
        s.*,
        (s.home_score IS NOT NULL AND s.away_score IS NOT NULL) AS played,
        CASE WHEN s.home_moneyline < 0 THEN -s.home_moneyline / (-s.home_moneyline + 100.0)
             ELSE 100.0 / (s.home_moneyline + 100.0) END AS p_home_raw,
        CASE WHEN s.away_moneyline < 0 THEN -s.away_moneyline / (-s.away_moneyline + 100.0)
             ELSE 100.0 / (s.away_moneyline + 100.0) END AS p_away_raw
    FROM schedules s
)
SELECT
    game_id, season, week, game_type, gameday, home_team, away_team, played,
    home_score, away_score,
    CASE WHEN played THEN home_score - away_score END                AS margin_home,
    spread_line, total_line, home_moneyline, away_moneyline,
    home_spread_odds, away_spread_odds, over_odds, under_odds,
    -- probabilidad de que gane el local según el mercado, ya sin el margen de la casa
    CASE WHEN home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
         THEN p_home_raw / (p_home_raw + p_away_raw) END             AS market_home_prob,
    CASE WHEN played AND spread_line IS NOT NULL THEN
        CASE WHEN home_score - away_score > spread_line THEN 'HOME'
             WHEN home_score - away_score < spread_line THEN 'AWAY'
             ELSE 'PUSH' END END                                     AS ats_winner,
    CASE WHEN played AND total_line IS NOT NULL THEN
        CASE WHEN home_score + away_score > total_line THEN 'OVER'
             WHEN home_score + away_score < total_line THEN 'UNDER'
             ELSE 'PUSH' END END                                     AS ou_result,
    home_rest, away_rest, roof, temp, wind, home_qb_name, away_qb_name
FROM base
"""


def main() -> None:
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 2021
    last = nfl.get_current_season()
    seasons = list(range(first, last + 1))
    DATA.mkdir(exist_ok=True)

    print(f"Descargando temporadas {first}-{last} de nflverse...")
    nfl.load_team_stats(seasons=seasons).write_parquet(DATA / "team_stats.parquet")
    nfl.load_schedules(seasons=seasons).write_parquet(DATA / "schedules.parquet")
    nfl.load_teams().write_parquet(DATA / "teams.parquet")  # nombres, colores y logos

    print("Construyendo nfl.duckdb...")
    try:
        con = duckdb.connect(str(DB))
    except duckdb.Error as e:
        # DuckDB bloquea el archivo mientras otra conexión lo tiene abierto: chat.py y api.py lo
        # abren en solo lectura, así que hay que cerrarlos antes de reconstruir la base.
        if "lock" in str(e).lower():
            sys.exit(
                f"No pude abrir {DB.name} para escritura:\n  {e}\n\n"
                "DuckDB bloquea el archivo mientras otra conexión lo tiene abierto.\n"
                "Cierra la web (uvicorn) y el chat (python chat.py) y vuelve a intentarlo."
            )
        raise
    for name in ("team_stats", "schedules", "teams"):
        path = (DATA / f"{name}.parquet").as_posix()
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_parquet('{path}')")
    con.execute(TEAM_GAMES_SQL)
    con.execute(GAMES_SQL)

    row1 = con.execute("SELECT count(*), min(season), max(season) FROM team_games").fetchone()
    if row1 is None:
        raise RuntimeError("team_games está vacía tras la construcción.")
    n, s_min, s_max = row1

    row2 = con.execute("SELECT count(*), count(spread_line) FROM games").fetchone()
    if row2 is None:
        raise RuntimeError("games está vacía tras la construcción.")
    n_games, n_lines = row2
    print(f"Listo: team_games tiene {n} filas ({s_min}-{s_max}).")
    print(f"       games tiene {n_games} juegos, {n_lines} con línea de apuestas.")
    con.close()


if __name__ == "__main__":
    main()
