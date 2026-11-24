#!/usr/bin/env python3
"""
listener.py — escucha un canal de Telegram con tu cuenta (Telethon) y reenvía las señales
al proxy por su webhook local. Sólo lee: nunca escribe en Telegram.

    python listener.py login      # una vez: pide teléfono, código y 2FA (los escribes tú)
    python listener.py channels   # lista tus canales con su id para rellenar TG_CHANNEL
    python listener.py            # escucha (lo que corre el servicio systemd)

Cada mensaje del canal (nuevo o editado) se guarda en MESSAGES_FILE (JSON por línea) con su
tipo (classify.py). Sólo las señales nuevas y recientes (SIGNAL_MAX_AGE_SEC) van al proxy;
una edición que convierte un mensaje en señal también se envía, una sola vez.

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

from classify import classify

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
MAX_AGE = float(os.environ.get("SIGNAL_MAX_AGE_SEC", "120"))


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} listener {msg}", flush=True)


def append(row: dict) -> None:
    with MESSAGES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def already_sent() -> set[int]:
    """Ids de mensaje ya enviados al proxy (sobrevive reinicios)."""
    sent: set[int] = set()
    if MESSAGES.exists():
        for line in MESSAGES.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("forwarded"):
                sent.add(int(row["msg_id"]))
    return sent


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
    sent = already_sent()
    c = client()
    await c.connect()
    if not await c.is_user_authorized():
        raise SystemExit("Sesión sin autorizar: ejecuta antes `python listener.py login`")
    chat = await c.get_entity(channel_ref(CHANNEL))
    log(f"escuchando «{getattr(chat, 'title', CHANNEL)}» -> {PROXY_URL}")

    async def handle(event, edited: bool) -> None:
        msg = event.message
        text = msg.message or ""
        info = classify(text)
        age = time.time() - msg.date.timestamp()
        row = {"ts": time.time(), "msg_id": msg.id, "date": msg.date.isoformat(), "edited": edited,
               "reply_to": msg.reply_to_msg_id, **info, "text": text, "forwarded": False}
        if info["type"] == "signal":
            if msg.id in sent:
                row["skip"] = "ya enviada"
            elif age > MAX_AGE:
                row["skip"] = f"vieja ({age:.0f}s > {MAX_AGE:.0f}s)"
            else:
                resp = await asyncio.to_thread(forward, text)
                row.update(forwarded=True, proxy=resp)
                sent.add(msg.id)
        append(row)
        extra = row.get("skip") or (row.get("proxy", {}).get("error") if row["forwarded"] else "")
        log(f"{info['type']:<10} {info.get('symbol') or '-':<12} msg {msg.id}"
            f"{' (editado)' if edited else ''}{' -> proxy' if row['forwarded'] else ''}"
            f"{f' [{extra}]' if extra else ''}")

    c.add_event_handler(lambda e: handle(e, False), events.NewMessage(chats=chat))
    c.add_event_handler(lambda e: handle(e, True), events.MessageEdited(chats=chat))
    await c.run_until_disconnected()


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
