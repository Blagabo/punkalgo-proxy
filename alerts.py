"""
alerts.py — avisos a Discord sólo para lo que pide tu atención (no aperturas ni cierres).

    from alerts import Alerts
    ALERTS = Alerts("proxy")
    ALERTS.alert("sl_missing:INJUSDT", "SL sin colocar", "…", level="critical")
    ALERTS.resolve("proxy_down", "Proxy de vuelta", "…")      # sólo si antes se avisó

Anti-spam:
  * `cooldown`: la misma clave no se repite antes de N segundos; las repeticiones se cuentan
    y salen en el siguiente aviso ("se repitió N veces").
  * `min_count` + `window`: errores pasajeros (red, 5xx, 429) sólo avisan si se repiten
    `min_count` veces dentro de `window` segundos.
  * `resolve`: un aviso de "todo bien otra vez" sólo si esa clave estaba en alerta.

El envío va en un hilo aparte con cola: un Discord lento o caído nunca frena al bot.
DISCORD_WEBHOOK_URL vacío = avisos desactivados. La URL es un secreto: sólo en el .env.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

COLORS = {"critical": 0xE5484D, "error": 0xE5484D, "warning": 0xF5A524, "ok": 0x30A46C, "info": 0x5B8DEF}
ICONS = {"critical": "🚨", "error": "❗", "warning": "⚠️", "ok": "✅", "info": "ℹ️"}
TRANSIENT = re.compile(r"network|timed? ?out|timeout|http-5\d\d|http-429|connection|temporarily|reset by peer", re.I)


def normalize(text: str) -> str:
    """Clave estable para un mensaje de error: sin números, ids ni precios."""
    return re.sub(r"[0-9a-f]{8,}|\d+(?:[.,]\d+)?", "#", text)[:160]


class Alerts:
    def __init__(self, source: str, url: str | None = None, bot: str | None = None,
                 state_path: Path | None = None, post: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.source = source
        self.url = (url if url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
        self.bot = bot or os.environ.get("BOT_NAME", "punkchainer")
        self.clock = clock
        self.lock = threading.Lock()
        self.state_path = state_path
        self.last: dict[str, float] = {}         # clave -> último envío
        self.suppressed: dict[str, int] = {}     # clave -> repeticiones calladas desde el último envío
        self.hits: dict[str, list[float]] = {}   # clave -> apariciones recientes (min_count)
        self.active: set[str] = set()            # claves en alerta (para resolve)
        self._load()
        self._custom = post is not None          # en tests: envío directo, sin red ni hilo
        self._post = post or self._post_discord
        self.q: queue.Queue[dict] = queue.Queue(maxsize=200)
        if post is None and self.url:
            threading.Thread(target=self._worker, daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self.url) or self._custom

    # ---- estado (sobrevive reinicios: tras un redespliegue no se repite lo ya avisado) ----
    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.last = {k: float(v) for k, v in (raw.get("last") or {}).items()}
            self.active = set(raw.get("active") or [])
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            cut = self.clock() - 7 * 86400
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last": {k: v for k, v in self.last.items() if v > cut},
                                       "active": sorted(self.active)}), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # ---- API ---------------------------------------------------------------------------------
    def alert(self, key: str, title: str, description: str = "", level: str = "error",
              cooldown: float = 1800, min_count: int = 1, window: float = 600,
              fields: dict[str, Any] | None = None) -> bool:
        """Avisa (o no, por el anti-spam). Devuelve True si el aviso se ha enviado."""
        if not self.enabled:
            return False
        now = self.clock()
        with self.lock:
            if min_count > 1:
                recent = [t for t in self.hits.get(key, []) if now - t <= window] + [now]
                self.hits[key] = recent[-min_count:]
                if len(recent) < min_count:
                    return False
            if now - self.last.get(key, 0.0) < cooldown:
                self.suppressed[key] = self.suppressed.get(key, 0) + 1
                return False
            repeats = self.suppressed.pop(key, 0)
            self.last[key] = now
            self.active.add(key)
            self._save()
        if repeats:
            description = f"{description}\n\n_(se repitió {repeats} {'vez' if repeats == 1 else 'veces'} desde el último aviso)_"
        self._send(level, title, description, fields)
        return True

    def resolve(self, key: str, title: str, description: str = "") -> bool:
        """Aviso de vuelta a la normalidad, sólo si esa clave estaba en alerta."""
        if not self.enabled:
            return False
        with self.lock:
            if key not in self.active:
                return False
            self.active.discard(key)
            self.last.pop(key, None)
            self.suppressed.pop(key, None)
            self._save()
        self._send("ok", title, description, None)
        return True

    def error_log(self, message: str) -> bool:
        """Para los logs de nivel ERROR: los pasajeros (red, 5xx, 429) sólo si se repiten."""
        transient = bool(TRANSIENT.search(message))
        return self.alert(f"log:{normalize(message)}", "Error en el bot", message[:1500],
                          level="warning" if transient else "error",
                          min_count=3 if transient else 1, window=900)

    # ---- envío -------------------------------------------------------------------------------
    def _send(self, level: str, title: str, description: str, fields: dict[str, Any] | None) -> None:
        embed: dict[str, Any] = {
            "title": f"{ICONS.get(level, '')} {title}"[:256],
            "description": description[:3500],
            "color": COLORS.get(level, COLORS["info"]),
            "footer": {"text": f"{self.bot} · {self.source}"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
        }
        if fields:
            embed["fields"] = [{"name": str(k)[:256], "value": str(v)[:1024] or "—", "inline": True}
                               for k, v in list(fields.items())[:10]]
        payload = {"username": "Punkchainer bot", "embeds": [embed]}
        if not self._custom:
            try:
                self.q.put_nowait(payload)
            except queue.Full:
                pass                                 # Discord caído mucho rato: se descarta
        else:
            self._post(payload)

    def _worker(self) -> None:
        while True:
            payload = self.q.get()
            for _ in range(3):
                try:
                    self._post_discord(payload)
                    break
                except Exception:
                    time.sleep(5)

    def _post_discord(self, payload: dict) -> None:
        resp = requests.post(self.url, json=payload, timeout=15)
        if resp.status_code == 429:                  # límite de Discord: espera lo que pida
            try:
                wait = float(resp.json().get("retry_after", 2))
            except ValueError:
                wait = 2.0
            time.sleep(min(wait, 60))
            raise RuntimeError("discord 429")
        resp.raise_for_status()


if __name__ == "__main__":                       # python alerts.py test  -> manda un aviso de prueba
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        a = Alerts("prueba")
        if not a.url:
            raise SystemExit("DISCORD_WEBHOOK_URL está vacío en el .env")
        a._post_discord({"username": "Punkchainer bot", "embeds": [{
            "title": "ℹ️ Prueba de avisos", "color": COLORS["info"],
            "description": "Si ves esto, los avisos del bot llegan a este canal.",
            "footer": {"text": f"{a.bot} · prueba"}}]})
        print("aviso de prueba enviado")
