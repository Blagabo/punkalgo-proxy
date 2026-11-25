#!/usr/bin/env python3
"""
test_trace.py — trazabilidad del paper: mejor/peor precio de cada operación (MFE/MAE) y
seguimiento "en sombra" tras el cierre hasta el SL original, el último objetivo o el tiempo.

    python3 test_trace.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import test_paper as tp  # noqa: E402  (fija el entorno e importa proxy)
from paper import PaperExchange  # noqa: E402

proxy = tp.proxy


def main() -> int:
    clock = tp.Clock()
    market = tp.FakeMarket(clock)
    journal = Path(os.environ["PAPER_JOURNAL_FILE"])
    outcomes = Path(os.environ["PAPER_OUTCOMES_FILE"])
    ex = PaperExchange(proxy.CFG, market, Path(os.environ["PAPER_STATE_FILE"]), journal,
                       log=proxy.log, clock=clock, error_cls=proxy.BitunixError)
    proxy.EX = ex
    proxy.STATE.positions, proxy.STATE.seen, proxy.STATE.daily = [], {}, {}
    rows = lambda p: [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
    results: list[tuple[bool, str]] = []

    # INJ short: entra, toca TP1 (5.9967), vuelve a BE y cierra
    market.tick("INJUSDT", 6.10)
    market.tick("INJUSDT", 6.105, high=6.11)           # la limit 6.102 se llena con la mecha
    proxy.handle_signal(tp.INJ, "test")
    proxy.monitor_once()
    proxy.monitor_once()
    market.tick("INJUSDT", 6.13, high=6.14, new_candle=True)   # peor momento: 6.14
    proxy.monitor_once()
    market.tick("INJUSDT", 5.995, new_candle=True)     # TP1
    proxy.monitor_once()
    proxy.monitor_once()
    market.tick("INJUSDT", 6.10, new_candle=True)      # vuelve: salta el BE
    proxy.monitor_once()
    proxy.monitor_once()

    j = rows(journal)[-1] if rows(journal) else {}
    results.append((j.get("close_reason") == "BE/SL", f"cerrada en BE tras TP1 ({j.get('close_reason')})"))
    results.append((j.get("best") == 5.995 and j.get("worst") == 6.14, f"MFE/MAE en el journal: mejor {j.get('best')}, peor {j.get('worst')}"))
    results.append((len(j.get("tps") or []) == 5, f"objetivos guardados en el journal: {j.get('tps')}"))
    o = rows(outcomes)
    results.append((len(o) == 1 and o[0]["state"] == "tracking" and o[0]["reach_tp"] == 1,
                    f"empieza el seguimiento en sombra con TP1 alcanzado: {o[-1] if o else None}"))
    results.append((len(ex.s["shadows"]) == 1, "una operación en sombra"))

    # sin posición: el monitor sigue el precio con track()
    market.tick("INJUSDT", 5.95, new_candle=True)      # pasa TP2 (5.981) y TP3 (5.9628)
    clock.t += 31
    proxy.monitor_once()
    sh = next(iter(ex.s["shadows"].values()))
    results.append((sh["reach_tp"] == 3, f"tras el cierre llega hasta el objetivo {sh['reach_tp']}"))
    market.tick("INJUSDT", 6.30, high=6.31, new_candle=True)   # SL original (6.296)
    clock.t += 31
    proxy.monitor_once()
    o = rows(outcomes)
    results.append((o[-1]["state"] == "sl" and o[-1]["reach_tp"] == 3 and not ex.s["shadows"],
                    f"seguimiento terminado por el SL original con 3/5 objetivos: {o[-1]['state']}"))

    # una operación que cierra en SL no se sigue: su resultado ya es definitivo
    market.tick("BNBUSDT", 599.5)
    proxy.handle_signal(tp.BNB, "test")
    proxy.monitor_once()
    market.tick("BNBUSDT", 589, new_candle=True)
    proxy.monitor_once()
    proxy.monitor_once()
    o = rows(outcomes)
    results.append((o[-1]["symbol"] == "BNBUSDT" and o[-1]["state"] == "sl" and o[-1]["reach_tp"] == 0
                    and not ex.s["shadows"], f"cierre en SL: resultado directo ({o[-1]['state']})"))

    # tiempo máximo de seguimiento
    ex.track_hours = 0.001
    market.tick("INJUSDT", 6.12, new_candle=True)      # short ejecutable al momento
    proxy.handle_signal(tp.INJ, "test")
    proxy.monitor_once()
    proxy.monitor_once()
    pid = next(iter(ex.s["positions"]))
    proxy.close_action({"symbol": "INJUSDT"})
    results.append((len(ex.s["shadows"]) == 1, "cierre manual: pasa a seguimiento"))
    clock.t += 60
    market.tick("INJUSDT", 6.05, new_candle=True)
    proxy.monitor_once()
    o = rows(outcomes)
    results.append((o[-1]["state"] == "timeout" and not ex.s["shadows"] and pid not in ex.s["positions"],
                    f"sin SL ni último objetivo a tiempo: {o[-1]['state']}"))

    print()
    ok = True
    for passed, label in results:
        ok &= passed
        print(f"  {'OK  ' if passed else 'FAIL'}  {label}")
    print("\ntest_trace", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
