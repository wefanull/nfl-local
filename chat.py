"""Chat en terminal con un modelo local (Ollama) que te da ESTADÍSTICAS de la NFL para que tú decidas.

El modelo solo consulta datos y te los explica: no apuesta ni decide por ti.

Uso:
    python chat.py
    python chat.py gemma4:e4b        # probar otro modelo
    (o define la variable NFL_MODEL)
    python chat.py --registro        # OPCIONAL: activa tu registro personal de apuestas (pide confirmación)
"""
import inspect
import os
import sys
from collections.abc import Callable
from typing import Any

import ollama

import bets
import tools

_cli = __name__ == "__main__"  # los argumentos solo se leen al correr "python chat.py"
REGISTRO = (_cli and "--registro" in sys.argv) or os.environ.get("NFL_REGISTRO") == "1"
_args = [a for a in sys.argv[1:] if not a.startswith("--")] if _cli else []
MODEL = _args[0] if _args else os.environ.get("NFL_MODEL", "granite4.2:8b")

# temperature baja = SQL y llamadas a herramientas más consistentes.
# num_ctx chico = menos RAM (algunos modelos traen un contexto enorme por defecto).
OPTIONS = {"temperature": 0.1, "num_ctx": 8192}

MAX_STEPS = 6  # tope de rondas modelo -> herramienta -> modelo por pregunta

SYSTEM = """Eres un analista de estadísticas de la NFL. Respondes en español.
Tu trabajo es darle datos al usuario para que ÉL tome sus propias decisiones. No apuestas ni decides por él.
Reglas:
- Nunca inventes números. Todo dato debe salir de una herramienta.
- Si necesitas saber qué columnas existen, llama primero a get_schema.
- Para analizar un juego por jugar usa game_report (mercado, contexto, forma de ambos equipos, base histórica).
- Para ver qué juegos hay y sus líneas usa upcoming_games. spread_line positivo = el LOCAL es favorito.
- Para forma reciente de un equipo usa team_recent_form; para comparar dos equipos, compare_teams.
- Para convertir una cuota en probabilidad usa implied_probability.
- Para cualquier otra pregunta, escribe SQL con run_sql sobre las tablas team_games o games.
- Si una herramienta devuelve un error, corrige la consulta y reintenta.
- No recomiendes apostar ni digas qué lado tomar, y nunca llames nada "seguro". Presenta los datos, qué dice el
  mercado, qué factores empujan cada lado y qué tan incierto es. La decisión es del usuario.
- Si la muestra es pequeña (pocos juegos), dilo claramente. Explica que el mercado ya incorpora gran parte de
  la información y que diferencias pequeñas frente a 50% suelen ser ruido."""

TOOLS: list[Callable[..., Any]] = [
    tools.game_report, tools.upcoming_games, tools.team_recent_form, tools.compare_teams,
    tools.implied_probability, tools.get_schema, tools.run_sql,
]
# Registro personal de apuestas: apagado por defecto. Solo anota; nunca apuesta por ti.
WRITE_TOOLS = {"log_bet", "set_closing_odds", "settle_open_bets"}
if REGISTRO:
    TOOLS += [bets.bet_summary, bets.log_bet, bets.set_closing_odds, bets.settle_open_bets]
    SYSTEM += "\n- Solo usa las herramientas de registro (log_bet, etc.) si el usuario pide anotar algo de forma explícita."
FUNCS: dict[str, Callable[..., Any]] = {f.__name__: f for f in TOOLS}


def confirm(name: str, args: dict) -> bool:
    """Pide permiso antes de que el modelo modifique tu registro de apuestas."""
    answer = input(f"  >> El modelo quiere ejecutar {name}({args}). ¿Permitir? [s/N] ").strip().lower()
    return answer in {"s", "si", "sí", "y", "yes"}


def call_tool(name: str, args: dict) -> str:
    """Ejecuta una herramienta pedida por el modelo; los errores vuelven como texto."""
    fn = FUNCS.get(name)
    if fn is None:
        return f"Error: la herramienta '{name}' no existe. Disponibles: {', '.join(FUNCS)}."
    params = inspect.signature(fn).parameters
    clean = {}
    for key, value in (args or {}).items():
        if key not in params:
            continue  # ignora argumentos inventados
        annotation = params[key].annotation
        # Los modelos chicos a veces mandan "5" o "44.5" como texto: se convierte aqui para que
        # el error salga claro en este punto en vez de reventar dentro de la herramienta.
        if annotation is int or annotation is float:
            try:
                value = int(value) if annotation is int else float(value)
            except (TypeError, ValueError):
                return (f"Error: el argumento '{key}' debe ser un número "
                        f"{'entero' if annotation is int else 'decimal'}.")
        clean[key] = value
    if name in WRITE_TOOLS and not confirm(name, clean):
        return "El usuario canceló esta acción. No se guardó nada."
    try:
        return str(fn(**clean))
    except TypeError as e:
        return f"Error en los argumentos de {name}: {e}"
    except Exception as e:
        return f"Error ejecutando {name}: {e}"


def ask(messages: list) -> str:
    for _ in range(MAX_STEPS):
        response = ollama.chat(model=MODEL, messages=messages, tools=TOOLS, options=OPTIONS)
        msg = response.message
        messages.append(msg)
        if not msg.tool_calls:
            return msg.content or "(el modelo no devolvió texto)"
        for call in msg.tool_calls:
            name, args = call.function.name, dict(call.function.arguments)
            print(f"  [herramienta] {name}({args})")
            messages.append({"role": "tool", "tool_name": name, "content": call_tool(name, args)})
    return "Llegué al límite de pasos sin una respuesta final. Prueba reformular la pregunta."


def main() -> None:
    if not tools.DB.exists():
        sys.exit("No encuentro nfl.duckdb. Corre primero: python fetch_data.py")
    print(f"Modelo: {MODEL}  |  registro de apuestas: {'activado' if REGISTRO else 'desactivado'}  (escribe 'salir' para terminar)\n")
    messages = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            question = input("tú> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in {"salir", "exit", "quit"}:
            break
        if not question:
            continue
        mark = len(messages)  # para poder deshacer el turno entero si el modelo falla
        messages.append({"role": "user", "content": question})
        try:
            print(f"\n{ask(messages)}\n")
        except ollama.ResponseError as e:
            print(f"\nError de Ollama: {e.error}")
            print("¿Descargaste el modelo? Prueba: ollama pull " + MODEL + "\n")
            # ask() puede haber dejado un tool_calls sin sus respuestas: se descarta el turno completo
            del messages[mark:]
        except Exception as e:  # p. ej. Ollama no está corriendo
            print(f"\nNo pude hablar con Ollama: {e}\n")
            del messages[mark:]


if __name__ == "__main__":
    main()
