#!/usr/bin/env python3
"""
listener.py — escucha un canal de Telegram con tu cuenta (Telethon) y reenvía las señales
al proxy por su webhook local. Sólo lee: nunca escribe en Telegram.

    python listener.py login      # una vez: pide teléfono, código y 2FA (los escribes tú)
    python listener.py channels   # lista tus canales con su id para rellenar TG_CHANNEL
    python listener.py            # escucha (lo que corre el servicio systemd)

Cada mensaje del canal (nuevo o editado) se guarda en MESSAGES_FILE (JSON por línea) con su
tipo (classify.py), cómo llegó (push de Telegram o sondeo) y cuánto tardó en llegar. Sólo las
señales recientes (SIGNAL_MAX_AGE_SEC) van al proxy; una edición que convierte un mensaje en
señal también se envía, una sola vez.

Trazabilidad: además de los avisos de Telegram, cada POLL_SEC revisa los últimos mensajes del
canal y procesa los que no hayan llegado (si Telegram se salta alguno, no se pierde). Escribe
su log en LISTENER_LOG_FILE y un latido en LISTENER_HEARTBEAT_FILE para saber si está vivo.

Variables (.env junto al script): TG_API_ID, TG_API_HASH (de my.telegram.org), TG_CHANNEL
(id numérico -100… o @usuario), TG_SESSION, PROXY_URL, WEBHOOK_TOKEN, MESSAGES_FILE,
SIGNAL_MAX_AGE_SEC.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from alerts import Alerts
from classify import classify
from logfile import append_line

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #")[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(Path(os.environ.get("PROXY_ENV_FILE", HERE / ".env")))

API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = os.environ.get("TG_API_HASH", "")
CHANNEL = os.environ.get("TG_CHANNEL", "").strip()
SESSION = os.environ.get("TG_SESSION", str(HERE / "tg_listener"))
PROXY_URL = os.environ.get(
    "PROXY_URL",
    f"http://127.0.0.1:{os.environ.get('PROXY_PORT', '8080')}{os.environ.get('WEBHOOK_PATH', '/webhook')}")
TOKEN = os.environ.get("WEBHOOK_TOKEN", "")
MESSAGES = Path(os.environ.get("MESSAGES_FILE", HERE / "tg_messages.jsonl"))
MAX_AGE = float(os.environ.get("SIGNAL_MAX_AGE_SEC", "1800"))
POLL_SEC = float(os.environ.get("POLL_SEC", "120"))
LOG_FILE = Path(os.environ.get("LISTENER_LOG_FILE", HERE / "listener.log"))
HEARTBEAT = Path(os.environ.get("LISTENER_HEARTBEAT_FILE", HERE / "listener_heartbeat.json"))
ALERTS = Alerts("listener", state_path=Path(os.environ.get("ALERTS_STATE_FILE", HERE / "alerts_listener.json")))
LATE_ALERT_SEC = 300
# Rechazos del proxy que son decisiones normales (frenos, duplicados…), no problemas
EXPECTED_REJECT = ("freno diario", "riesgo abierto", "duplicada", "ya está gestionado", "límite de",
                   "margen sumado", "del mercado")


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} listener {msg}"
    print(line, flush=True)
    append_line(LOG_FILE, line)


def append(row: dict) -> None:
    with MESSAGES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def known_ids() -> tuple[set[int], set[int]]:
    """(ids ya vistos, ids ya enviados al proxy), leídos del archivo: sobreviven reinicios."""
    seen: set[int] = set()
    sent: set[int] = set()
    if MESSAGES.exists():
        for line in MESSAGES.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            seen.add(int(row["msg_id"]))
            if row.get("forwarded"):
                sent.add(int(row["msg_id"]))
    return seen, sent


def beat(state: dict) -> None:
    """Latido: lo lee sync.py para avisar en la web si el listener se cae o se desconecta."""
    try:
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps({**state, "ts": time.time()}), encoding="utf-8")
        tmp.replace(HEARTBEAT)
    except OSError:
        pass


def forward(text: str) -> dict:
    try:
        resp = requests.post(PROXY_URL, data=text.encode("utf-8"), timeout=30,
                             headers={"Content-Type": "text/plain; charset=utf-8", "X-Webhook-Token": TOKEN})
        try:
            return {"status": resp.status_code, **resp.json()}
        except ValueError:
            return {"status": resp.status_code, "ok": False, "error": resp.text[:200]}
    except requests.RequestException as exc:
        return {"status": None, "ok": False, "error": str(exc)}


def warn_forward(symbol: str, resp: dict, age: float) -> None:
    """Aviso si una señal no llegó al proxy o el proxy la rechazó por un problema (no por los frenos)."""
    if resp.get("status") is None or (resp.get("status") or 0) >= 500:
        ALERTS.alert("forward_fail", "Señal NO entregada al bot",
                     f"{symbol}: el proxy no respondió ({resp.get('error') or resp.get('status')}). "
                     f"Esa señal no se ha operado.", level="critical", cooldown=600)
    elif not resp.get("ok"):
        err = str(resp.get("error") or "")
        if not any(x in err.lower() for x in EXPECTED_REJECT):
            from alerts import normalize
            ALERTS.alert(f"reject:{normalize(err)}", f"Señal de {symbol} rechazada",
                         f"El bot no la ha operado: {err}", level="warning", cooldown=1800)


def channel_ref(raw: str):
    return int(raw) if raw.lstrip("-").isdigit() else raw


def client():
    from telethon import TelegramClient
    if not API_ID or not API_HASH:
        raise SystemExit("Define TG_API_ID y TG_API_HASH en .env (my.telegram.org → API development tools)")
    return TelegramClient(SESSION, API_ID, API_HASH)


async def cmd_login() -> None:
    async with client() as c:          # start() pide teléfono, código y 2FA por consola
        await c.start()
        me = await c.get_me()
        log(f"sesión guardada para {me.first_name} (id {me.id}) en {SESSION}.session")


async def cmd_channels() -> None:
    async with client() as c:
        await c.start()
        async for d in c.iter_dialogs():
            if d.is_channel:
                print(f"{d.id:>16}  {d.title}")


async def cmd_run() -> None:
    from telethon import events
    if not CHANNEL:
        raise SystemExit("Define TG_CHANNEL en .env (python listener.py channels)")
    seen, sent = known_ids()
    c = client()
    await c.connect()
    if not await c.is_user_authorized():
        ALERTS.alert("tg_unauthorized", "Sesión de Telegram caducada",
                     "El listener no puede leer el canal: no entran señales. En el VPS: "
                     "`docker compose run --rm listener python listener.py login`.",
                     level="critical", cooldown=6 * 3600)
        time.sleep(2)                                 # deja salir el aviso antes de terminar
        raise SystemExit("Sesión sin autorizar: ejecuta antes `python listener.py login`")
    chat = await c.get_entity(channel_ref(CHANNEL))
    state = {"started": time.time(), "connected": True, "last_msg_ts": None, "last_msg_id": None,
             "last_poll": None, "polled": 0, "late": 0}
    log(f"escuchando «{getattr(chat, 'title', CHANNEL)}» -> {PROXY_URL} "
        f"(señales hasta {MAX_AGE:.0f}s de antigüedad, sondeo cada {POLL_SEC:.0f}s)")
    beat(state)
    lock = asyncio.Lock()                      # push y sondeo no procesan el mismo mensaje a la vez

    async def process(msg, edited: bool, source: str) -> None:
        async with lock:
            if source == "poll" and msg.id in seen:
                return
            await handle(msg, edited, source)

    async def handle(msg, edited: bool, source: str) -> None:
        text = msg.message or ""
        info = classify(text)
        stamp = (msg.edit_date or msg.date) if edited else msg.date
        age = time.time() - stamp.timestamp()
        seen.add(msg.id)
        state.update(last_msg_ts=time.time(), last_msg_id=msg.id)
        if source == "poll":
            state["polled"] += 1
        if age > 60:
            state["late"] += 1
        row = {"ts": time.time(), "msg_id": msg.id, "date": msg.date.isoformat(), "edited": edited,
               "edit_date": msg.edit_date.isoformat() if msg.edit_date else None, "source": source,
               "delay_s": round(age, 1), "reply_to": msg.reply_to_msg_id, **info, "text": text,
               "forwarded": False}
        if info["type"] == "signal":
            if msg.id in sent:
                row["skip"] = "ya enviada"
            elif age > MAX_AGE:
                row["skip"] = f"vieja ({age:.0f}s > {MAX_AGE:.0f}s)"
            else:
                resp = await asyncio.to_thread(forward, text)
                row.update(forwarded=True, proxy=resp)
                warn_forward(info.get("symbol") or "?", resp, age)
            if age > LATE_ALERT_SEC and "skip" not in row:
                ALERTS.alert("late_signal", "Señales llegando con retraso",
                             f"La señal de {info.get('symbol')} llegó {age / 60:.0f} min tarde"
                             f"{' (recuperada por el sondeo)' if source == 'poll' else ''}. "
                             f"Puede ser Telegram o la conexión del VPS.", level="warning", cooldown=3600)
                sent.add(msg.id)
        append(row)
        extra = row.get("skip") or (row.get("proxy", {}).get("error") if row["forwarded"] else "")
        log(f"{info['type']:<10} {info.get('symbol') or '-':<12} msg {msg.id}"
            f"{' (editado)' if edited else ''}{' [sondeo]' if source == 'poll' else ''}"
            f" +{age:.0f}s{' -> proxy' if row['forwarded'] else ''}{f' [{extra}]' if extra else ''}")
        beat(state)

    async def poll_loop() -> None:
        """Red de seguridad: procesa los mensajes recientes que Telegram no haya avisado."""
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                recent = [m async for m in c.iter_messages(chat, limit=20)]
                for m in sorted(recent, key=lambda m: m.id):
                    if m.id not in seen:
                        log(f"msg {m.id} no llegó por push: lo recupera el sondeo")
                        await process(m, False, "poll")
                state.update(last_poll=time.time(), connected=c.is_connected())
            except Exception as exc:          # sin red: se reintenta en la siguiente vuelta
                state["connected"] = c.is_connected()
                log(f"sondeo falló: {exc!r}")
            beat(state)

    c.add_event_handler(lambda e: process(e.message, False, "push"), events.NewMessage(chats=chat))
    c.add_event_handler(lambda e: process(e.message, True, "push"), events.MessageEdited(chats=chat))
    poller = asyncio.create_task(poll_loop())
    try:
        await c.run_until_disconnected()
    finally:
        poller.cancel()
        state["connected"] = False
        beat(state)
        log("desconectado de Telegram")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    runner = {"login": cmd_login, "channels": cmd_channels, "run": cmd_run}.get(cmd)
    if not runner:
        print(__doc__)
        return 2
    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
