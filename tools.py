"""Herramientas que el modelo puede llamar. Todas leen nfl.duckdb en modo solo-lectura.

Los docstrings importan: Ollama los usa para explicarle cada herramienta al modelo.
"""
import threading
from pathlib import Path

import duckdb

DB = Path(__file__).resolve().parent / "nfl.duckdb"
MAX_ROWS = 50
SQL_TIMEOUT = 15  # segundos máximos para una consulta de run_sql
# Juego por jugar: sin marcador Y con fecha de hoy en adelante. Si fetch_data.py no se ha corrido
# después de una jornada, esos juegos ya no aparecen como "por jugar".
UPCOMING = "NOT played AND CAST(gameday AS DATE) >= current_date"

# Métricas que se promedian en team_recent_form / compare_teams
METRICS = [
    "points_for", "points_against", "total_yards", "pass_epa", "rush_epa",
    "interceptions_thrown", "fumbles_lost", "sacks_taken", "sacks_made", "penalties",
]


def _con() -> duckdb.DuckDBPyConnection:
    # read_only + sin acceso externo: aunque el modelo genere SQL raro, no puede
    # modificar la base ni leer/escribir archivos del disco.
    return duckdb.connect(str(DB), read_only=True, config={"enable_external_access": False})


def _fmt(columns: list[str], rows: list[tuple]) -> str:
    def cell(v):
        return f"{v:.2f}" if isinstance(v, float) else str(v)

    lines = [" | ".join(columns), " | ".join("---" for _ in columns)]
    lines += [" | ".join(cell(v) for v in row) for row in rows]
    return "\n".join(lines)


def _num(v, decimals: int = 1, sign: bool = False) -> str:
    """Formatea un número; None (dato faltante) -> 'n/d' en vez de tronar."""
    if v is None:
        return "n/d"
    return f"{v:+.{decimals}f}" if sign else f"{v:.{decimals}f}"


def get_schema() -> str:
    """Describe las tablas disponibles y sus columnas. Llámala antes de escribir SQL.

    Returns:
        Texto con las columnas de team_games, las temporadas disponibles y una fila de ejemplo.
    """
    con = _con()
    cols = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'team_games' ORDER BY ordinal_position"
    ).fetchall()
    seasons = con.execute("SELECT min(season), max(season) FROM team_games").fetchone()
    if seasons is None:
        raise RuntimeError("team_games está vacía. Corre fetch_data.py primero.")
    sample = con.execute("SELECT * FROM team_games ORDER BY gameday DESC LIMIT 1")
    names = [d[0] for d in sample.description]
    row = sample.fetchone()
    if row is None:
        raise RuntimeError("team_games está vacía. Corre fetch_data.py primero.")
    con.close()
    return (
        "Tabla principal: team_games (una fila por equipo y juego ya jugado).\n"
        "Columnas: " + ", ".join(f"{n} ({t})" for n, t in cols) + "\n"
        f"Temporadas: {seasons[0]} a {seasons[1]}. season_type: REG (regular) o POST (playoffs).\n"
        "Líneas en team_games (perspectiva del equipo): team_spread (-3.5 = favorito por 3.5; +3.5 = underdog), "
        "ats_result (COVER / NO_COVER / PUSH), total_line y ou_result (OVER / UNDER / PUSH).\n"
        "Los equipos se identifican con su abreviatura (KC, SF, BUF, LA...).\n"
        "Tabla games (una fila por juego, jugado o por jugar): spread_line (POSITIVO = el LOCAL es favorito), "
        "total_line, home_moneyline, away_moneyline, market_home_prob (prob. de que gane el local según el mercado, "
        "sin margen de la casa), margin_home (local - visitante), ats_winner (HOME / AWAY / PUSH), "
        "ou_result, played (false = juego por jugar).\n"
        "Otras tablas: schedules (calendario crudo) y team_stats (138 columnas por equipo y juego).\n"
        "Ejemplo de fila: " + str(dict(zip(names, row)))
    )


def _form(con, team: str, last_n: int, season: int) -> dict | None:
    # season = 0 -> los últimos N juegos jugados, aunque crucen de una temporada a otra
    avgs = ", ".join(f"avg({m}) AS {m}" for m in METRICS)
    sql = f"""
        SELECT count(*) AS games, count(*) FILTER (WHERE points_for > points_against) AS wins,
               count(*) FILTER (WHERE points_for = points_against) AS ties,
               count(*) FILTER (WHERE ats_result = 'COVER') AS ats_wins,
               count(*) FILTER (WHERE ats_result = 'NO_COVER') AS ats_losses,
               count(*) FILTER (WHERE ou_result = 'OVER') AS overs,
               count(*) FILTER (WHERE ou_result = 'UNDER') AS unders,
               {avgs}
        FROM (SELECT * FROM team_games WHERE team = ? AND (? = 0 OR season = ?)
              ORDER BY gameday DESC LIMIT ?)
    """
    cur = con.execute(sql, [team.upper(), season, season, last_n])
    names = [d[0] for d in cur.description]
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return dict(zip(names, row))


def _record(games: int, wins: int, ties: int) -> str:
    """Récord G-P, o G-P-E si hubo empates."""
    return f"{wins}-{games - wins - ties}" + (f"-{ties}" if ties else "")


def team_recent_form(team: str, last_n: int = 5, season: int = 0) -> str:
    """Resume el rendimiento reciente de un equipo: promedios de sus últimos juegos.

    Args:
        team: Abreviatura del equipo, por ejemplo KC, SF, BUF, DAL.
        last_n: Cuántos de los últimos juegos promediar.
        season: Temporada (por ejemplo 2025). Usa 0 para los juegos más recientes sin importar la temporada.

    Returns:
        Récord, récord contra el spread (ATS), over/under y promedios (puntos, yardas, EPA, entregas...).
    """
    con = _con()
    form = _form(con, team, last_n, season)
    con.close()
    if form is None:
        return f"No hay datos para {team} en esa temporada. Revisa la abreviatura con get_schema()."
    games = form.pop("games")
    record = _record(games, form.pop("wins"), form.pop("ties"))
    ats = f"{form.pop('ats_wins')}-{form.pop('ats_losses')}"
    ou = f"{form.pop('overs')} over / {form.pop('unders')} under"
    stats = ", ".join(f"{k}={_num(v, 2)}" for k, v in form.items())
    return (f"{team.upper()} en sus últimos {games} juegos: {record}. "
            f"Contra el spread (cubrió-no cubrió): {ats}. Totales: {ou}. Promedios: {stats}")


def compare_teams(team_a: str, team_b: str, last_n: int = 5, season: int = 0) -> str:
    """Compara lado a lado el rendimiento reciente de dos equipos.

    Args:
        team_a: Abreviatura del primer equipo, por ejemplo KC.
        team_b: Abreviatura del segundo equipo, por ejemplo BUF.
        last_n: Cuántos de los últimos juegos de cada equipo promediar.
        season: Temporada (por ejemplo 2025). Usa 0 para los juegos más recientes sin importar la temporada.

    Returns:
        Tabla con los promedios de cada equipo.
    """
    con = _con()
    forms = {t.upper(): _form(con, t, last_n, season) for t in (team_a, team_b)}
    con.close()
    missing = [t for t, f in forms.items() if f is None]
    if missing:
        return f"No hay datos para: {', '.join(missing)}. Revisa las abreviaturas con get_schema()."
    assert all(f is not None for f in forms.values())
    cols = ["metric"] + list(forms)
    keys = ["games", "wins", "ties", "ats_wins", "ats_losses", "overs", "unders"] + METRICS
    rows = [(k, *[forms[t][k] for t in forms]) for k in keys]  # type: ignore[index]
    return _fmt(cols, rows)


def run_sql(query: str) -> str:
    """Ejecuta una consulta SQL de solo lectura (DuckDB) sobre la base de la NFL.

    Args:
        query: Una sola sentencia SELECT (o WITH ... SELECT). Máximo 50 filas de resultado.

    Returns:
        Tabla de resultados, o el mensaje de error para que puedas corregir la consulta.
    """
    q = query.strip().rstrip(";").strip()
    if not q.lower().startswith(("select", "with")) or ";" in q:
        return "Error: solo se permite una sentencia SELECT (o WITH ... SELECT)."
    con = _con()
    timer = threading.Timer(SQL_TIMEOUT, con.interrupt)  # corta consultas que tardan demasiado
    timer.start()
    try:
        cur = con.execute(q)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:  # el modelo ve el error y puede reintentar
        if not timer.is_alive():
            return f"Error: la consulta tardó más de {SQL_TIMEOUT} s y se canceló. Simplifícala o agrega filtros."
        return f"Error de SQL: {e}"
    finally:
        timer.cancel()
        con.close()
    note = f"\n(mostrando las primeras {MAX_ROWS} filas)" if len(rows) > MAX_ROWS else ""
    return _fmt(cols, rows[:MAX_ROWS]) + note


# ----------------------------------------------------------------------------- mercado y reportes

def _prob(odds: int) -> float:
    """Probabilidad implícita (con margen de la casa) de una cuota americana."""
    return -odds / (-odds + 100) if odds < 0 else 100 / (odds + 100)


def _row(con, sql: str, params: list | None = None) -> dict | None:
    cur = con.execute(sql, params or [])
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else None


def implied_probability(odds: int) -> str:
    """Convierte una cuota americana en la probabilidad implícita que le asigna la casa de apuestas.

    Args:
        odds: Cuota americana, por ejemplo -110, +150 o -250.

    Returns:
        Probabilidad implícita (incluye el margen de la casa) y la cuota decimal equivalente.
    """
    odds = int(odds)
    if -100 < odds < 100:
        return "Cuota inválida: las cuotas americanas son <= -100 o >= +100."
    p = _prob(odds)
    decimal = 1 + (odds / 100 if odds > 0 else 100 / -odds)
    return (f"Cuota {odds:+d}: probabilidad implícita {p:.1%} (decimal {decimal:.2f}). "
            "Incluye el margen de la casa, así que las dos caras de un mercado suman más de 100%.")


def upcoming_games(limit: int = 16) -> str:
    """Lista los próximos juegos que ya tienen línea, con su game_id.

    Args:
        limit: Máximo de juegos a mostrar.

    Returns:
        Tabla con game_id, fecha, equipos, spread (positivo = local favorito), total, moneylines
        y probabilidad de victoria del local según el mercado (sin margen de la casa).
    """
    limit = max(1, min(int(limit), 40))
    con = _con()
    try:
        cur = con.execute(
            f"""SELECT game_id, gameday, away_team || ' @ ' || home_team AS game, spread_line, total_line,
                      home_moneyline, away_moneyline, round(market_home_prob, 3) AS market_home_prob
               FROM games WHERE {UPCOMING} AND spread_line IS NOT NULL
               ORDER BY gameday, game_id LIMIT ?""", [limit])
        cols, rows = [d[0] for d in cur.description], cur.fetchall()
    finally:
        con.close()
    if not rows:
        return "No hay juegos por jugar con línea en la base. Corre fetch_data.py para actualizar."
    return "spread_line positivo = el LOCAL es favorito.\n" + _fmt(cols, rows)


def base_rates(con, spread: float | None, total: float | None) -> dict:
    """Base histórica (temporada regular): qué tan seguido cubren favoritos con spread parecido (±1.5)
    y pasan el over juegos con total parecido (±3). Solo incluye las claves con datos."""
    out = {}
    if spread is not None:  # 0.0 es un pick'em valido, no un dato ausente
        n, rate = con.execute(
            """SELECT count(*) FILTER (WHERE ats_winner != 'PUSH'),
                      avg(CASE WHEN ats_winner = CASE WHEN spread_line > 0 THEN 'HOME' ELSE 'AWAY' END
                               THEN 1.0 ELSE 0.0 END) FILTER (WHERE ats_winner != 'PUSH')
               FROM games WHERE played AND game_type = 'REG' AND spread_line != 0
                 AND abs(abs(spread_line) - ?) <= 1.5""", [abs(spread)]).fetchone()
        if n:
            out["fav_cover"] = {"n": n, "rate": rate}
    if total is not None:
        n, rate = con.execute(
            """SELECT count(*) FILTER (WHERE ou_result != 'PUSH'),
                      avg(CASE WHEN ou_result = 'OVER' THEN 1.0 ELSE 0.0 END) FILTER (WHERE ou_result != 'PUSH')
               FROM games WHERE played AND game_type = 'REG' AND total_line IS NOT NULL
                 AND abs(total_line - ?) <= 3""", [total]).fetchone()
        if n:
            out["over"] = {"n": n, "rate": rate}
    return out


def head_to_head(con, home: str, away: str, limit: int = 3) -> list[dict]:
    """Últimos enfrentamientos entre dos equipos (cualquier localía), del más reciente al más viejo."""
    cur = con.execute(
        """SELECT gameday, away_team, away_score, home_team, home_score FROM games
           WHERE played AND ((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?))
           ORDER BY gameday DESC LIMIT ?""", [home, away, away, home, limit])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def game_report(team: str = "", game_id: str = "", last_n: int = 5) -> str:
    """Reporte de estadísticas de un juego por jugar: mercado, contexto, forma de ambos equipos y base histórica.

    Args:
        team: Abreviatura de un equipo (KC, BUF...). Se usa su próximo juego. Déjalo vacío si das game_id.
        game_id: Id exacto del juego (de upcoming_games), por ejemplo 2026_04_NE_BUF. Opcional.
        last_n: Cuántos de los últimos juegos de cada equipo promediar.

    Returns:
        Reporte con líneas del mercado, descanso/clima/quarterbacks, forma reciente de cada equipo
        (récord, ATS, over/under, promedios), cuántas veces cubrieron juegos históricos parecidos y
        los últimos enfrentamientos.
    """
    last_n = max(1, min(int(last_n), 10))
    con = _con()
    try:
        if game_id:
            g = _row(con, "SELECT * FROM games WHERE game_id = ?", [game_id])
        elif team:
            t = team.upper()
            g = _row(con, f"SELECT * FROM games WHERE {UPCOMING} AND (home_team = ? OR away_team = ?) "
                          "ORDER BY gameday LIMIT 1", [t, t])
        else:
            return "Indica un equipo (team) o un game_id. Usa upcoming_games para ver los juegos."
        if g is None:
            return "No encontré ese juego. Revisa la abreviatura o usa upcoming_games."
        home, away = g["home_team"], g["away_team"]
        out = [f"REPORTE: {away} @ {home} - {g['gameday']} (semana {g['week']}, {g['game_type']}), id {g['game_id']}"]
        if g["played"]:
            out.append(f"Este juego ya se jugó: {away} {g['away_score']} - {home} {g['home_score']}. "
                       "Para un reporte previo usa un juego por jugar (upcoming_games).")
            return "\n".join(out)

        # 1) Mercado
        sp = g["spread_line"]
        if sp is None:
            out.append("MERCADO: todavía no hay línea para este juego en la base.")
        else:
            fav = "Pick'em (sin favorito)" if sp == 0 else f"{home if sp > 0 else away} favorito por {abs(sp):g}"
            out.append(f"MERCADO: {fav}. Total: {g['total_line']}. Moneyline: {home} {_num(g['home_moneyline'], 0, True)}, "
                       f"{away} {_num(g['away_moneyline'], 0, True)}.")
            mp = g["market_home_prob"]
            if mp is not None:
                out.append(f"  Probabilidad de victoria según el mercado (sin margen de la casa): {home} {mp:.1%}, {away} {1 - mp:.1%}.")

        # 2) Contexto
        ctx = [f"descanso (días): {home} {g['home_rest']}, {away} {g['away_rest']}"]
        if g["roof"]:
            ctx.append(f"estadio: {g['roof']}")
        if g["temp"] is not None:
            ctx.append(f"temperatura {g['temp']}°F, viento {g['wind']} mph")
        if g["home_qb_name"] or g["away_qb_name"]:
            ctx.append(f"QB listados: {home} {g['home_qb_name']}, {away} {g['away_qb_name']} (confirma lesiones aparte)")
        out.append("CONTEXTO: " + "; ".join(ctx) + ".")

        # 3) Forma reciente
        out.append(f"FORMA RECIENTE (últimos {last_n} juegos de cada uno):")
        this_season = con.execute("SELECT max(season) FROM team_games").fetchone()[0]  # type: ignore[index]
        for tm in (home, away):
            form = _form(con, tm, last_n, 0)
            if form is None:
                out.append(f"  {tm}: sin datos.")
                continue
            games_n = form.pop("games")
            record = _record(games_n, form.pop("wins"), form.pop("ties"))
            ats = f"{form.pop('ats_wins')}-{form.pop('ats_losses')}"
            ou = f"{form.pop('overs')} over/{form.pop('unders')} under"
            stats = ", ".join(f"{k}={_num(v)}" for k, v in form.items())
            in_season = con.execute("SELECT count(*) FROM team_games WHERE team = ? AND season = ?",
                                    [tm, this_season]).fetchone()[0]  # type: ignore[index]
            warn = f" [solo {in_season} juegos esta temporada: el resto es de la anterior]" if in_season < games_n else ""
            out.append(f"  {tm}: {record}, ATS {ats}, {ou}. {stats}{warn}")

        # 4) Base histórica: juegos parecidos de temporada regular
        rates = base_rates(con, sp, g["total_line"])
        base = []
        if "fav_cover" in rates:
            base.append(f"favoritos con spread parecido (±1.5) cubrieron {rates['fav_cover']['rate']:.1%} "
                        f"({rates['fav_cover']['n']} juegos)")
        if "over" in rates:
            base.append(f"juegos con total parecido (±3) pasaron el over {rates['over']['rate']:.1%} "
                        f"({rates['over']['n']} juegos)")
        if base:
            out.append("BASE HISTÓRICA (temporadas en la base): " + "; ".join(base) + ".")

        # 5) Últimos enfrentamientos
        h2h = head_to_head(con, home, away)
        if h2h:
            out.append("ÚLTIMOS ENFRENTAMIENTOS: " + " | ".join(
                f"{x['gameday']}: {x['away_team']} {x['away_score']} @ {x['home_team']} {x['home_score']}" for x in h2h))
    finally:
        con.close()
    out.append("NOTA: son datos descriptivos con muestras chicas; diferencias pequeñas frente a 50% suelen ser ruido. La línea del mercado ya incorpora gran parte de esta información.")
    return "\n".join(out)


if __name__ == "__main__":
    # Autoprueba rápida: python tools.py
    print(get_schema(), end="\n\n")
    print(team_recent_form("KC"), end="\n\n")
    print(compare_teams("KC", "BUF"), end="\n\n")
    print(run_sql("SELECT team, round(avg(points_for),1) AS ppg FROM team_games "
                  "WHERE season = 2025 AND season_type = 'REG' GROUP BY team ORDER BY ppg DESC LIMIT 5"))
    print(implied_probability(-110), end="\n\n")
    print(upcoming_games(3), end="\n\n")
    print(game_report("BUF"), end="\n\n")
    print(run_sql("DROP TABLE team_games"))
    print(run_sql("SELECT * FROM read_csv('/etc/passwd')"))
