#!/usr/bin/env python3
"""
test_guards.py — freno de pérdida diaria, tope de riesgo abierto y cierres manuales,
en modo PAPER contra el mercado guionizado de test_paper.py (sin red).

    python3 test_guards.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["DAILY_LOSS_LIMIT_PCT"] = "1"      # freno al caer un 1% en el día
os.environ["MAX_OPEN_RISK_PCT"] = "0.8"       # con 0.5% por operación cabe una sola a la vez

import test_paper as tp  # noqa: E402  (fija el resto del entorno e importa proxy)
from paper import PaperExchange  # noqa: E402

proxy = tp.proxy

SOL_WAIT = """#SOL/USDT - Long 🟢

Entrada: 150
Stop Loss: 147

🎯 Objetivo 1: 153

Apalancamiento: x20
"""
tp.PAIRS["SOLUSDT"] = {"symbol": "SOLUSDT", "basePrecision": 2, "quotePrecision": 2, "minTradeVolume": "0.01",
                       "maxLeverage": 50, "symbolStatus": "OPEN", "isApiSupported": True}


def main() -> int:
    clock = tp.Clock()
    market = tp.FakeMarket(clock)
    journal = Path(os.environ["PAPER_JOURNAL_FILE"])
    proxy.EX = PaperExchange(proxy.CFG, market, Path(os.environ["PAPER_STATE_FILE"]), journal,
                             log=proxy.log, clock=clock, error_cls=proxy.BitunixError)
    proxy.STATE.positions, proxy.STATE.seen, proxy.STATE.daily = [], {}, {}
    results: list[tuple[bool, str]] = []
    rows = lambda: [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []

    # ---- tope de riesgo abierto -----------------------------------------------------------
    market.tick("INJUSDT", 6.12)                       # short ejecutable al momento
    r1 = proxy.handle_signal(tp.INJ, "test")
    proxy.monitor_once()
    results.append((r1.get("ok") is True and proxy.STATE.positions[-1]["status"] == "OPEN", "INJ abierta (0.5% de riesgo)"))
    g = proxy.guards_payload(proxy.EX.equity()[0])
    results.append((g["open_risk_pct"] is not None and 0.45 < g["open_risk_pct"] < 0.55, f"riesgo abierto {g['open_risk_pct']}%"))

    market.tick("BNBUSDT", 599.5)
    r2 = proxy.handle_signal(tp.BNB, "test")
    results.append((r2.get("ok") is False and "riesgo abierto" in r2.get("error", ""),
                    f"BNB rechazada por el tope de 0.8%: {r2.get('error')}"))

    # ---- cierre manual de una -------------------------------------------------------------
    market.tick("INJUSDT", 6.05)
    st, rep = proxy.close_action({"symbol": "injusdt", "origin": "test"})
    rec = next(p for p in proxy.STATE.positions if p["symbol"] == "INJUSDT")
    row = rows()[-1] if rows() else {}
    results.append((st == 200 and rep["done"][0]["action"] == "closed" and rec["status"] == "CLOSED"
                    and "cierre manual" in rec["note"], f"INJ cerrada a mano ({st}, {rec['note']})"))
    results.append((row.get("close_reason") == "MANUAL" and row.get("pnl", 0) > 0,
                    f"journal: cierre MANUAL con PnL {row.get('pnl')}"))
    results.append((not proxy.EX.s["positions"] and not proxy.EX.s["tps"], "sin posición ni TPs vivos en el paper"))

    # ---- ya cabe otra; cerrar todo (abierta + entrada en espera) -----------------------------
    r3 = proxy.handle_signal(tp.BNB, "test")
    proxy.monitor_once()
    results.append((r3.get("ok") is True, f"BNB aceptada tras liberar riesgo: {r3.get('error') or 'ok'}"))
    proxy.CFG.max_open_risk_pct = 0                     # para que entre la segunda
    market.tick("SOLUSDT", 152)                         # long a 150 con el precio en 152: espera
    r4 = proxy.handle_signal(SOL_WAIT, "test")
    results.append((r4.get("ok") is True and proxy.STATE.positions[-1]["status"] == "WAITING_FILL", "SOL esperando entrada"))
    st, rep = proxy.close_action({"all": True, "origin": "test"})
    actions = sorted(d["action"] for d in rep["done"])
    results.append((st == 200 and actions == ["cancelled", "closed"] and not proxy.STATE.active(),
                    f"cerrar todo: {actions}"))
    st, rep = proxy.close_action({"all": True})
    results.append((st == 404, "cerrar todo sin nada activo responde 404"))

    # ---- freno diario -------------------------------------------------------------------------
    proxy.STATE.daily = {}
    proxy.note_day_equity(1000.0)
    proxy.note_day_equity(995.0)
    results.append((not proxy.guards_payload(995.0)["halted"], "−0,5% en el día: sin freno"))
    proxy.note_day_equity(989.9)
    g = proxy.guards_payload(989.9)
    results.append((g["halted"] and g["day_start_equity"] == 1000.0 and abs(g["day_change_pct"] + 1.01) < 1e-6,
                    f"−1,01% en el día: freno activo ({g['day_change_pct']}%)"))
    market.tick("INJUSDT", 6.12)
    r5 = proxy.handle_signal(tp.INJ, "test")
    results.append((r5.get("ok") is False and "freno diario" in r5.get("error", ""), f"señal rechazada: {r5.get('error')}"))
    proxy.STATE.daily = {"2000-01-01": {"start_equity": 1000.0, "halted": 1}}
    results.append((proxy.risk_guard({"equity": 989.0, "entry": 1, "sl": 1.01, "qty": 1}, None) is None,
                    "al día siguiente (UTC) el freno ya no aplica"))

    # ---- rutas HTTP con token --------------------------------------------------------------
    proxy.CFG.webhook_token = "tok"
    st, _, _ = proxy.http_dispatch("POST", "/api/close-all", {}, {}, "{}")
    results.append((st == 401, "/api/close-all sin token: 401"))
    st, body, _ = proxy.http_dispatch("POST", "/api/close", {"X-Webhook-Token": "tok"}, {}, '{"symbol": "XRPUSDT"}')
    results.append((st == 404 and body["ok"] is False, "/api/close con token y par sin operación: 404"))

    print()
    ok = True
    for passed, label in results:
        ok &= passed
        print(f"  {'OK  ' if passed else 'FAIL'}  {label}")
    print("\ntest_guards", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
