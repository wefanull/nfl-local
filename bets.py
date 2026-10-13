"""Registro OPCIONAL de apuestas (solo anota; nunca apuesta por ti).

En chat.py está DESACTIVADO por defecto (actívalo con --registro).
Las apuestas se guardan en bets.db (SQLite), separado de nfl.duckdb: así fetch_data.py puede
reconstruir los datos sin tocar tu historial, y el SQL libre del modelo sigue siendo solo-lectura.

Uso desde la terminal (sin modelo):
    python bets.py            # resumen
    python bets.py selftest   # prueba con una base temporal
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import duckdb

from tools import _prob, implied_probability, upcoming_games  # _prob para el CLV; las otras, para la autoprueba

BASE = Path(__file__).resolve().parent
NFL_DB = BASE / "nfl.duckdb"
BETS_DB = BASE / "bets.db"

BET_TYPES = ("spread", "moneyline", "over", "under")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    game_id       TEXT NOT NULL,
    bet_type      TEXT NOT NULL,      -- spread | moneyline | over | under
    pick          TEXT NOT NULL,      -- abreviatura del equipo, u OVER / UNDER
    line          REAL,               -- spread del equipo apostado (-3.5 = favorito) o total
    odds          INTEGER NOT NULL,   -- cuota americana (-110, +150...)
    stake         REAL NOT NULL,
    note          TEXT,
    closing_odds  INTEGER,            -- cuota de cierre, para medir CLV
    status        TEXT NOT NULL DEFAULT 'open',   -- open | win | loss | push
    profit        REAL
)
"""


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(BETS_DB)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def _profit(odds: int, stake: float, status: str) -> float:
    if status == "push":
        return 0.0
    if status == "loss":
        return -stake
    return stake * odds / 100 if odds > 0 else stake * 100 / -odds


def _table(columns: list[str], rows: list[tuple]) -> str:
    lines = [" | ".join(columns), " | ".join("---" for _ in columns)]
    lines += [" | ".join("" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in r)
              for r in rows]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- registro

def log_bet(game_id: str, bet_type: str, pick: str, odds: int, stake: float,
            line: float = 0.0, note: str = "") -> str:
    """Registra una apuesta nueva (queda 'open' hasta que se califique).

    Args:
        game_id: Id del juego tal como sale en upcoming_games, por ejemplo 2026_04_NE_BUF.
        bet_type: Uno de: spread, moneyline, over, under.
        pick: Abreviatura del equipo (spread/moneyline), o OVER / UNDER.
        odds: Cuota americana de la apuesta, por ejemplo -110 o +150.
        stake: Monto apostado.
        line: Para spread, el spread DEL EQUIPO apostado (-3.5 si es favorito, +3.5 si es underdog).
            Para over/under, el total (por ejemplo 44.5). En moneyline usa 0.
        note: Nota opcional (por qué haces la apuesta).

    Returns:
        Confirmación con el id de la apuesta.
    """
    bet_type, pick = str(bet_type).lower(), str(pick).upper()
    odds, stake, line = int(odds), float(stake), float(line)
    if bet_type not in BET_TYPES:
        return f"Error: bet_type debe ser uno de {', '.join(BET_TYPES)}."
    if bet_type in ("over", "under"):
        pick = bet_type.upper()
    if -100 < odds < 100:
        return "Error: cuota americana inválida."
    if stake <= 0:
        return "Error: el monto debe ser mayor que 0."
    if bet_type in ("spread", "over", "under") and line == 0.0:
        return "Error: para spread, over y under debes indicar la línea (line)."
    con = duckdb.connect(str(NFL_DB), read_only=True, config={"enable_external_access": False})
    try:
        game = con.execute("SELECT home_team, away_team, played FROM games WHERE game_id = ?",
                           [game_id]).fetchone()
    finally:
        con.close()
    if game is None:
        return f"Error: no existe el game_id '{game_id}'. Usa upcoming_games para ver los ids."
    if game[2]:
        return "Error: ese juego ya se jugó."
    if bet_type in ("spread", "moneyline") and pick not in (game[0], game[1]):
        return f"Error: pick debe ser {game[0]} o {game[1]} para ese juego."
    db = _db()
    with db:
        cur = db.execute(
            "INSERT INTO bets (created_at, game_id, bet_type, pick, line, odds, stake, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), game_id, bet_type, pick,
             line if bet_type != "moneyline" else None, odds, stake, note or None))
    db.close()
    return f"Apuesta #{cur.lastrowid} registrada: {bet_type} {pick} {line:+g} @ {odds:+d}, monto {stake:g}."


def set_closing_odds(bet_id: int, closing_odds: int) -> str:
    """Guarda la cuota de cierre de una apuesta, para medir si conseguiste mejor precio que el mercado (CLV).

    Args:
        bet_id: Id de la apuesta.
        closing_odds: Cuota americana justo antes de que empiece el juego, por ejemplo -120.

    Returns:
        Confirmación.
    """
    closing_odds = int(closing_odds)
    if -100 < closing_odds < 100:
        return "Error: cuota americana inválida."
    db = _db()
    with db:
        n = db.execute("UPDATE bets SET closing_odds = ? WHERE id = ?", (closing_odds, int(bet_id))).rowcount
    db.close()
    return f"Cuota de cierre guardada en la apuesta #{bet_id}." if n else f"Error: no existe la apuesta #{bet_id}."


def _grade(bet: sqlite3.Row, game: tuple) -> str:
    """Devuelve win/loss/push. game = (home, away, hs, as)."""
    home, away, hs, as_ = game
    total = hs + as_
    if bet["bet_type"] == "moneyline":
        if hs == as_:
            return "push"
        winner = home if hs > as_ else away
        return "win" if bet["pick"] == winner else "loss"
    if bet["bet_type"] == "spread":
        margin = (hs - as_) if bet["pick"] == home else (as_ - hs)
        adj = margin + bet["line"]
        return "win" if adj > 0 else "loss" if adj < 0 else "push"
    diff = total - bet["line"]
    if diff == 0:
        return "push"
    return "win" if (diff > 0) == (bet["bet_type"] == "over") else "loss"


def settle_open_bets() -> str:
    """Califica automáticamente las apuestas abiertas cuyos juegos ya se jugaron (usa los marcadores de la base).

    Returns:
        Resumen de las apuestas calificadas. Antes conviene correr fetch_data.py para traer marcadores nuevos.
    """
    db = _db()
    open_bets = db.execute("SELECT * FROM bets WHERE status = 'open'").fetchall()
    if not open_bets:
        db.close()
        return "No hay apuestas abiertas."
    con = duckdb.connect(str(NFL_DB), read_only=True, config={"enable_external_access": False})
    out, pending = [], 0
    try:
        for b in open_bets:
            game = con.execute(
                "SELECT home_team, away_team, home_score, away_score FROM games "
                "WHERE game_id = ? AND played", [b["game_id"]]).fetchone()
            if game is None:
                pending += 1
                continue
            status = _grade(b, game)
            profit = _profit(b["odds"], b["stake"], status)
            with db:
                db.execute("UPDATE bets SET status = ?, profit = ? WHERE id = ?", (status, profit, b["id"]))
            out.append(f"#{b['id']} {b['game_id']} {b['bet_type']} {b['pick']}: {status} ({profit:+.2f})")
    finally:
        con.close()
        db.close()
    msg = "\n".join(out) if out else "Ninguna apuesta se pudo calificar todavía."
    return msg + (f"\n{pending} apuesta(s) siguen abiertas (juego no jugado o sin marcador)." if pending else "")


def bet_summary() -> str:
    """Resumen de tus apuestas: récord, monto apostado, ganancia, ROI y CLV promedio.

    Returns:
        Texto con los totales y las últimas apuestas.
    """
    db = _db()
    rows = db.execute("SELECT * FROM bets ORDER BY id").fetchall()
    db.close()
    if not rows:
        return "Todavía no hay apuestas registradas."
    done = [r for r in rows if r["status"] != "open"]
    wins = sum(r["status"] == "win" for r in done)
    losses = sum(r["status"] == "loss" for r in done)
    pushes = sum(r["status"] == "push" for r in done)
    staked = sum(r["stake"] for r in done)
    profit = sum(r["profit"] or 0 for r in done)
    roi = f"{profit / staked:.1%}" if staked else "n/a"
    clv = [_prob(r["closing_odds"]) - _prob(r["odds"]) for r in rows if r["closing_odds"] is not None]
    clv_txt = (f"{sum(clv) / len(clv) * 100:+.2f} puntos porcentuales sobre {len(clv)} apuesta(s) "
               f"(positivo = conseguiste mejor precio que el cierre)") if clv else "sin datos (usa set_closing_odds)"
    last = _table(["id", "game_id", "tipo", "pick", "línea", "cuota", "monto", "estado", "ganancia"],
                  [(r["id"], r["game_id"], r["bet_type"], r["pick"], r["line"], r["odds"], r["stake"],
                    r["status"], r["profit"]) for r in rows[-10:]])
    return (f"Apuestas: {len(rows)} ({len(rows) - len(done)} abiertas). Calificadas: {wins}-{losses}-{pushes} (G-P-E).\n"
            f"Apostado: {staked:.2f}  Ganancia: {profit:+.2f}  ROI: {roi}\nCLV promedio: {clv_txt}\n\n{last}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        import tempfile
        BETS_DB = Path(tempfile.mkdtemp()) / "bets_test.db"
        print(implied_probability(-110))
        print(implied_probability(+150), end="\n\n")
        print(upcoming_games(3), end="\n\n")
        gid_row = duckdb.connect(str(NFL_DB), read_only=True).execute(
            "SELECT game_id FROM games WHERE NOT played AND spread_line IS NOT NULL ORDER BY gameday LIMIT 1").fetchone()
        assert gid_row is not None
        gid = gid_row[0]
        print("Registrar a futuro:", log_bet(gid, "over", "OVER", -110, 10, line=44.5))
        played = duckdb.connect(str(NFL_DB), read_only=True).execute(
            "SELECT game_id, home_team, home_score - away_score, spread_line FROM games "
            "WHERE played AND spread_line IS NOT NULL ORDER BY gameday DESC LIMIT 1").fetchone()
        assert played is not None
        print("Error esperado (ya jugado):", log_bet(played[0], "moneyline", played[1], -150, 10))
        print("Error esperado (sin línea):", log_bet(gid, "spread", "KC", -110, 10))
        # Para probar la calificación, se inserta a mano una apuesta sobre un juego ya jugado
        db = _db()
        with db:
            db.execute("INSERT INTO bets (created_at, game_id, bet_type, pick, line, odds, stake, closing_odds) "
                       "VALUES ('test', ?, 'spread', ?, ?, -110, 10, -125)", (played[0], played[1], -played[3]))
        db.close()
        print("Calificar:\n" + settle_open_bets(), end="\n\n")
        print(bet_summary())
    else:
        print(bet_summary())
