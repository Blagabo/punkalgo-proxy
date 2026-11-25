#!/usr/bin/env python3
"""
test_alerts.py — avisos a Discord sin red: anti-spam, errores pasajeros, resolución, vigilante
del sync y los avisos concretos del proxy y del listener.

    python3 test_alerts.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="alerts_test_"))
os.environ.update({
    "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SECRET_KEY": "k", "BOT_OWNER_ID": "u",
    "SYNC_CACHE_FILE": str(TMP / "c.json"), "SYNC_LOG_FILE": str(TMP / "s.log"),
    "LISTENER_LOG_FILE": str(TMP / "l.log"), "LISTENER_HEARTBEAT_FILE": str(TMP / "hb.json"),
    "MESSAGES_FILE": str(TMP / "m.jsonl"), "DISCORD_WEBHOOK_URL": "",
})

import test_paper as tp  # noqa: E402  (entorno del paper e importa proxy)
import alerts  # noqa: E402
import listener  # noqa: E402
import sync  # noqa: E402

proxy = tp.proxy


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def make(clock: Clock, sent: list, source: str = "test", state: Path | None = None) -> alerts.Alerts:
    return alerts.Alerts(source, url="", post=sent.append, clock=clock, state_path=state)


def title(p: dict) -> str:
    return p["embeds"][0]["title"]


def main() -> int:
    results: list[tuple[bool, str]] = []
    clock, sent = Clock(), []
    a = make(clock, sent, state=TMP / "state.json")

    # ---- anti-spam -------------------------------------------------------------------------
    a.alert("k", "Algo falla", "detalle", cooldown=1800)
    a.alert("k", "Algo falla", "detalle", cooldown=1800)
    a.alert("k", "Algo falla", "detalle", cooldown=1800)
    results.append((len(sent) == 1, f"la misma alerta no se repite dentro del cooldown ({len(sent)} enviada)"))
    clock.t += 1801
    a.alert("k", "Algo falla", "detalle", cooldown=1800)
    desc = sent[-1]["embeds"][0]["description"]
    results.append((len(sent) == 2 and "se repitió 2 veces" in desc, "pasado el cooldown vuelve a avisar y cuenta las repeticiones"))
    b = make(clock, [], state=TMP / "state.json")
    results.append((not b.alert("k", "Algo falla", cooldown=1800), "el cooldown sobrevive a un reinicio (estado en disco)"))

    # ---- resolución --------------------------------------------------------------------------
    n = len(sent)
    a.resolve("otra", "Todo bien")
    results.append((len(sent) == n, "no hay aviso verde de algo que no estaba en alerta"))
    a.resolve("k", "Vuelve a funcionar")
    results.append((len(sent) == n + 1 and sent[-1]["embeds"][0]["color"] == alerts.COLORS["ok"], "aviso verde al resolverse"))
    a.resolve("k", "Vuelve a funcionar")
    results.append((len(sent) == n + 1, "y sólo una vez"))

    # ---- errores de log: pasajeros sólo si se repiten ------------------------------------------
    sent.clear()
    a.error_log("monitor INJUSDT: [network] Read timed out (1234)")
    a.error_log("monitor INJUSDT: [network] Read timed out (5678)")
    results.append((not sent, "un error de red aislado no avisa (ni dos)"))
    a.error_log("monitor INJUSDT: [network] Read timed out (9999)")
    results.append((len(sent) == 1 and sent[0]["embeds"][0]["color"] == alerts.COLORS["warning"],
                    "al tercero en 15 min avisa (como aviso, no error)"))
    a.error_log("plan inválido para XYZUSDT: XYZUSDT no existe en Bitunix futuros")
    results.append((len(sent) == 2 and sent[1]["embeds"][0]["color"] == alerts.COLORS["error"], "un error real avisa a la primera"))
    results.append((alerts.normalize("orden 123 a 0.0045") == alerts.normalize("orden 456 a 0.0099"),
                    "los números no crean claves distintas"))

    # ---- proxy ---------------------------------------------------------------------------------
    sent.clear()
    proxy.ALERTS = make(clock, sent, "proxy")
    proxy.log("WARNING", "INJUSDT: ya hay una operación gestionada; señal ignorada")
    proxy.log("INFO", "ORDEN INJUSDT SHORT qty=25 @ 6.1")
    results.append((not sent, "aperturas, avisos y rechazos normales no van a Discord"))
    proxy.log("ERROR", "orden rechazada por Bitunix en INJUSDT: saldo insuficiente")
    results.append((len(sent) == 1 and "saldo insuficiente" in sent[0]["embeds"][0]["description"], "un ERROR del proxy sí"))

    sent.clear()

    class Boom:
        def place_position_tp_sl(self, *a, **k):
            raise proxy.BitunixError("10002", "parámetro inválido", "tpsl")

    real_ex = proxy.EX
    proxy.EX = Boom()
    proxy.ensure_position_sl({"id": "x", "symbol": "INJUSDT", "side": "SHORT", "sl": 6.296, "tps": [6.0],
                              "price_places": 4, "position_id": "P1", "sl_order_id": None})
    proxy.EX = real_ex
    results.append((len(sent) == 1 and "SIN stop loss" in title(sent[0]) and sent[0]["embeds"][0]["color"] == alerts.COLORS["critical"],
                    f"SL sin colocar: un único aviso crítico ({[title(p) for p in sent]})"))

    sent.clear()
    proxy.CFG.daily_loss_limit_pct = 1.0
    proxy.STATE.daily = {}
    proxy.note_day_equity(1000.0)
    proxy.note_day_equity(985.0)
    proxy.note_day_equity(980.0)
    results.append((len(sent) == 1 and "Freno diario" in title(sent[0]), "freno diario: un aviso al activarse"))
    proxy.CFG.daily_loss_limit_pct = 0

    sent.clear()
    proxy.STATE.counters["equity_peak"] = 0
    proxy.watch_drawdown(1000.0)
    proxy.watch_drawdown(960.0)
    results.append((not sent, "−4% desde el máximo: sin aviso (umbral 5%)"))
    proxy.watch_drawdown(945.0)
    results.append((len(sent) == 1 and "Drawdown" in title(sent[0]), "−5,5%: aviso de drawdown"))
    proxy.watch_drawdown(990.0)
    results.append((len(sent) == 2 and sent[1]["embeds"][0]["color"] == alerts.COLORS["ok"], "se recupera: aviso verde"))

    # ---- listener ------------------------------------------------------------------------------
    sent.clear()
    listener.ALERTS = make(clock, sent, "listener")
    listener.warn_forward("INJUSDT", {"status": 400, "ok": False, "error": "freno diario activo (pérdida…)"}, 3)
    listener.warn_forward("INJUSDT", {"status": 400, "ok": False, "error": "riesgo abierto 9.9 USDT pasaría del tope"}, 3)
    listener.warn_forward("INJUSDT", {"status": 200, "ok": True}, 3)
    results.append((not sent, "rechazos por los frenos y señales operadas: sin aviso"))
    listener.warn_forward("INJUSDT", {"status": None, "ok": False, "error": "Connection refused"}, 3)
    results.append((len(sent) == 1 and "NO entregada" in title(sent[0]), "proxy caído al reenviar: aviso crítico"))
    listener.warn_forward("XYZUSDT", {"status": 400, "ok": False, "error": "plan inválido: XYZUSDT no existe en Bitunix"}, 3)
    results.append((len(sent) == 2 and "rechazada" in title(sent[1]), "rechazo por un problema: aviso"))

    # ---- vigilante del sync --------------------------------------------------------------------
    sent.clear()
    sync.ALERTS = make(clock, sent, "vigilante")
    dog = sync.Watchdog(clock=clock)
    dog.proxy(False)
    clock.t += 60
    dog.proxy(False)
    results.append((not sent, "proxy caído 1 min: aún sin aviso"))
    clock.t += 90
    dog.proxy(False)
    results.append((len(sent) == 1 and "no responde" in title(sent[0]), "proxy caído > 2 min: aviso crítico"))
    dog.proxy(True)
    results.append((len(sent) == 2 and sent[1]["embeds"][0]["color"] == alerts.COLORS["ok"], "proxy de vuelta: aviso verde"))

    sent.clear()
    clock.t += 400                                      # el vigilante ya lleva un rato arrancado
    dog.listener({})
    dog.listener({"ts": clock.t - 30, "connected": True})
    results.append((not sent, "listener latiendo (o desactivado): sin aviso"))
    dog.listener({"ts": clock.t - 700, "connected": True})
    results.append((len(sent) == 1 and "listener" in title(sent[0]), "listener sin latido > 6 min: aviso"))
    dog.listener({"ts": clock.t - 10, "connected": True})
    results.append((len(sent) == 2, "listener de vuelta: aviso verde"))

    sent.clear()
    dog.sync_result("HTTP 503")
    clock.t += 301
    dog.sync_result("HTTP 503")
    results.append((len(sent) == 1 and "web no se actualiza" in title(sent[0]), "Supabase fallando > 5 min: aviso"))

    disabled = alerts.Alerts("x", url="")
    results.append((not disabled.enabled and not disabled.alert("k", "t"), "sin DISCORD_WEBHOOK_URL no se envía nada"))

    print()
    ok = True
    for passed, label in results:
        ok &= passed
        print(f"  {'OK  ' if passed else 'FAIL'}  {label}")
    print("\ntest_alerts", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
