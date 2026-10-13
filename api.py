"""Web + API para ver el análisis de la NFL en el navegador (solo lectura).

    pip install -r requirements.txt
    uvicorn api:app --reload        # luego abre http://127.0.0.1:8000
"""
from pathlib import Path

import duckdb
import ollama
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import chat  # mismo prompt y herramientas que el chat de terminal
import tools

app = FastAPI(title="NFL local")
INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def query(sql: str, params: list | None = None) -> list[dict]:
    """Consulta de solo lectura que devuelve filas como diccionarios (NaN -> None)."""
    con = tools._con()
    try:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()
    return [{c: (None if isinstance(v, float) and v != v else v) for c, v in zip(cols, r)} for r in rows]


def summary(rows: list[dict]) -> dict:
    n = len(rows)
    count = lambda key, val: sum(r[key] == val for r in rows)
    def avg(key):
        vals = [r[key] for r in rows if r[key] is not None]  # sin dato != 0
        return round(sum(vals) / len(vals), 1) if vals else None
    return {
        "n": n,
        "wins": sum(r["points_for"] > r["points_against"] for r in rows),
        "ties": sum(r["points_for"] == r["points_against"] for r in rows),
        "ats": [count("ats_result", "COVER"), count("ats_result", "NO_COVER")],
        "ou": [count("ou_result", "OVER"), count("ou_result", "UNDER")],
        **{k: avg(k) for k in ("points_for", "points_against", "total_yards", "pass_epa", "rush_epa", "turnovers")},
    }


@app.get("/")
def index():
    return FileResponse(INDEX)


app.mount("/static", StaticFiles(directory=INDEX.parent), name="static")  # JS local: funciona sin internet


@app.get("/api/week")
def week(limit: int = 20):
    """Próximos juegos con línea."""
    return query(f"""SELECT game_id, week, gameday, away_team, home_team, spread_line, total_line,
                           home_moneyline, away_moneyline, market_home_prob
                    FROM games WHERE {tools.UPCOMING} AND spread_line IS NOT NULL
                    ORDER BY gameday, game_id LIMIT ?""", [max(1, min(limit, 40))])


@app.get("/api/game/{game_id}")
def game(game_id: str, last_n: int = 5):
    """Reporte de un juego: mercado, forma reciente de ambos equipos, base histórica y últimos enfrentamientos."""
    last_n = max(1, min(last_n, 10))
    found = query("SELECT * FROM games WHERE game_id = ?", [game_id])
    if not found:
        raise HTTPException(404, "Juego no encontrado")
    g = found[0]
    home, away = g["home_team"], g["away_team"]

    teams = {}
    for t in (home, away):
        rows = query("""SELECT gameday, season, opponent, is_home, points_for, points_against, total_yards,
                               pass_epa, rush_epa, interceptions_thrown + fumbles_lost AS turnovers,
                               ats_result, ou_result
                        FROM team_games WHERE team = ? ORDER BY gameday DESC LIMIT ?""", [t, last_n])
        rows.reverse()  # cronológico, para las gráficas
        teams[t] = {"games": rows, "summary": summary(rows)}

    con = tools._con()  # misma lógica que game_report del chat, para que ambos digan lo mismo
    try:
        base = tools.base_rates(con, g["spread_line"], g["total_line"])
        h2h = tools.head_to_head(con, home, away)
    finally:
        con.close()
    return {"game": g, "teams": teams, "base": base, "h2h": h2h}


@app.get("/api/teams")
def teams():
    """Nombre, color y logo de cada equipo (necesita haber corrido fetch_data.py después de esta actualización)."""
    try:
        rows = query("SELECT team_abbr, team_name, team_color, team_logo_espn FROM teams")
    except (duckdb.CatalogException, duckdb.BinderException):
        # La tabla 'teams', o sus columnas de logos, solo existen si fetch_data.py se corrio despues
        # de agregar los logos: devolvemos {} para que la web siga funcionando sin ellos.
        # Cualquier otro error (base bloqueada, corrupta...) ya NO se silencia: sale como error 500.
        return {}
    return {r["team_abbr"]: {"name": r["team_name"], "color": r["team_color"], "logo": r["team_logo_espn"]} for r in rows}


WEB_FORMAT = ("\n- Tu respuesta se muestra en una página web que renderiza Markdown: usa tablas Markdown para comparar "
              "equipos o listar varios datos, **negritas** para lo importante y listas cortas. No uses HTML ni "
              "envuelvas las tablas en bloques de código. Sé breve.")


class ChatIn(BaseModel):
    question: str
    history: list[dict] = []
    game_id: str | None = None


@app.post("/api/chat")
def chat_endpoint(body: ChatIn):
    """Chat con el modelo local de Ollama usando las herramientas de solo lectura."""
    if chat.REGISTRO:
        raise HTTPException(400, "El chat web no funciona con NFL_REGISTRO=1: esas herramientas piden confirmación en la terminal.")
    ctx = f"[El usuario está viendo el reporte del juego {body.game_id}.]\n" if body.game_id else ""
    past = [{"role": m["role"], "content": m["content"]} for m in body.history[-10:]
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    messages = [{"role": "system", "content": chat.SYSTEM + WEB_FORMAT}, *past, {"role": "user", "content": ctx + body.question}]
    try:
        reply = chat.ask(messages)
    except ollama.ResponseError as e:
        raise HTTPException(502, f"Ollama respondió con un error: {e.error}. ¿Descargaste el modelo {chat.MODEL}?")
    except Exception as e:
        raise HTTPException(503, f"No pude hablar con Ollama: {e}")
    used = [m["tool_name"] for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    return {"reply": reply, "tools": used}
