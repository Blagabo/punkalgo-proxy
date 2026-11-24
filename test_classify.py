#!/usr/bin/env python3
"""test_classify.py — los tipos de mensaje reales del canal.  python3 test_classify.py"""

from __future__ import annotations

import sys

from classify import classify

CASES = [
    ("""#INJ/USDT - Short 🔴

Entrada: 6.102
Stop Loss: 6.29602

🎯 Objetivo 1: 5.99674
🎯 Objetivo 2: 5.98095
🎯 Objetivo 3: 5.96279
🎯 Objetivo 4: 5.94191
🎯 Objetivo 5: 5.9179

Apalancamiento: x20""", {"type": "signal", "symbol": "INJUSDT", "side": "SHORT"}),
    ("""🚀🔥 PHAROS/USDT
📈 Pumpeando +6.34% 🟢
⏱️ Últimas 24h: +9.69%
💰 Precio: 0.6339 USDT""", {"type": "pump", "symbol": "PHAROSUSDT", "change_pct": 6.34, "price": 0.6339}),
    ("""#BNB/USDT

🎯 Objetivo 1 Tocado ✅

Beneficio: 33.7084% 📈
Periodo: 17 Minutos ⏰

🛡️ Mueve tu SL a la entrada (Break Even)""",
     {"type": "target_hit", "symbol": "BNBUSDT", "target": 1, "profit_pct": 33.7084,
      "minutes": 17, "move_to_be": True}),
    ("""#BNB/USDT

🎯 Objetivo 2 Tocado ✅

Beneficio: 38.7646% 📈
Periodo: 35 Minutos ⏰""",
     {"type": "target_hit", "symbol": "BNBUSDT", "target": 2, "profit_pct": 38.7646,
      "minutes": 35, "move_to_be": False}),
    ("""#SOL/USDT

🎯 Objetivo 3 Tocado ✅

Beneficio: 51.2% 📈
Periodo: 2 Horas 5 Minutos ⏰""", {"type": "target_hit", "target": 3, "minutes": 125}),
    ("""#ORDI/USDT 

Stop Loss Alcanzado ⛔

Pérdida: -66.1903% 📉""", {"type": "sl_hit", "symbol": "ORDIUSDT", "loss_pct": -66.1903}),
    ("Buenos días a todos, hoy mercado lateral", {"type": "other", "symbol": None}),
]


def main() -> int:
    ok = True
    for text, expected in CASES:
        got = classify(text)
        bad = {k: (v, got.get(k)) for k, v in expected.items() if got.get(k) != v}
        ok &= not bad
        print(f"  {'OK  ' if not bad else 'FAIL'}  {expected['type']:<10} {text.splitlines()[0][:30]!r}"
              + (f"  {bad}" if bad else ""))
    print("\ntest_classify", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
