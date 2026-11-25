#!/usr/bin/env python3
"""
test_paper.py — ciclo completo del bot en modo PAPER contra un mercado guionizado.

Sin red: el "mercado" (pares, último precio y velas de 1 m) es falso y lo mueve el test.
Comprueba fill maker al tocar la entrada, mechas entre polls, TP parciales, break-even,
SL conservador, sizing por riesgo, journal en R y persistencia del estado.

    python3 test_paper.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="paper_test_"))
os.environ.update({
    "BROKER": "paper", "DRY_RUN": "false", "WEBHOOK_TOKEN": "", "LOG_LEVEL": "INFO",
    "STATE_FILE": str(TMP / "state.json"), "LOG_FILE": str(TMP / "proxy.log"),
    "PAPER_STATE_FILE": str(TMP / "paper.json"), "PAPER_JOURNAL_FILE": str(TMP / "journal.jsonl"),
    "RUNTIME_FILE": str(TMP / "runtime.json"),
    "PAPER_OUTCOMES_FILE": str(TMP / "outcomes.jsonl"),
    "PAPER_EQUITY": "1000", "SIZING_MODE": "risk", "RISK_PCT": "0.5",
    "PAPER_SLIPPAGE_PCT": "0", "PAPER_TAKER_FEE_PCT": "0.06", "PAPER_MAKER_FEE_PCT": "0.02",
    "MAX_PRICE_DEVIATION_PCT": "0", "MAX_OPEN_POSITIONS": "10", "MAX_TOTAL_MARGIN_PCT": "0",
    "FAST_ARM_SEC": "0", "DEDUPE_WINDOW_SEC": "0",
    "BITUNIX_API_KEY": "", "BITUNIX_API_SECRET": "",
})

import proxy  # noqa: E402
from paper import PaperExchange  # noqa: E402

INJ = """#INJ/USDT - Short 🔴

Entrada: 6.102
Stop Loss: 6.29602

🎯 Objetivo 1: 5.99674
🎯 Objetivo 2: 5.98095
🎯 Objetivo 3: 5.96279
🎯 Objetivo 4: 5.94191
🎯 Objetivo 5: 5.9179

Apalancamiento: x20
"""

BNB = """#BNB/USDT - Long 🟢

Entrada: 600
Stop Loss: 590

🎯 Objetivo 1: 605
🎯 Objetivo 2: 610

Apalancamiento: x20
"""

PAIRS = {
    "INJUSDT": {"symbol": "INJUSDT", "basePrecision": 2, "quotePrecision": 4, "minTradeVolume": "0.1",
                "maxLeverage": 50, "symbolStatus": "OPEN", "isApiSupported": True},
    "BNBUSDT": {"symbol": "BNBUSDT", "basePrecision": 3, "quotePrecision": 2, "minTradeVolume": "0.01",
                "maxLeverage": 50, "symbolStatus": "OPEN", "isApiSupported": True},
}


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class FakeMarket:
    """Último precio + vela de 1 m en curso, movidos a mano por el test."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.candles: dict[str, list[dict]] = {}
        self.last: dict[str, float] = {}

    def trading_pairs(self, force: bool = False) -> dict:
        return PAIRS

    def pair(self, symbol: str) -> dict:
        return PAIRS[symbol]

    def last_price(self, symbol: str) -> float:
        return self.last[symbol]

    def klines(self, symbol: str, interval: str = "1m", limit: int = 3) -> list[dict]:
        return list(reversed(self.candles.get(symbol, [])[-limit:]))

    def tick(self, symbol: str, last: float, high: float | None = None, low: float | None = None,
             new_candle: bool = False) -> None:
        """Mueve el precio; high/low son extremos alcanzados dentro de la vela en curso."""
        self.clock.t += 5
        rows = self.candles.setdefault(symbol, [])
        if new_candle or not rows:
            rows.append({"time": str(int(self.clock.t * 1000)), "open": last,
                         "high": last, "low": last, "close": last})
        c = rows[-1]
        c["high"] = max(float(c["high"]), high or last, last)
        c["low"] = min(float(c["low"]), low or last, last)
        c["close"] = last
        self.last[symbol] = last


def journal() -> list[dict]:
    path = Path(os.environ["PAPER_JOURNAL_FILE"])
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def main() -> int:
    clock = Clock()
    market = FakeMarket(clock)
    ex = PaperExchange(proxy.CFG, market, Path(os.environ["PAPER_STATE_FILE"]),
                       Path(os.environ["PAPER_JOURNAL_FILE"]), log=proxy.log, clock=clock,
                       error_cls=proxy.BitunixError)
    proxy.EX = ex
    proxy.STATE.positions, proxy.STATE.seen = [], {}
    results: list[tuple[bool, str]] = []

    # ---- 1. INJ short: la limit espera por encima del mercado ---------------------------
    market.tick("INJUSDT", 6.03)
    resp = proxy.handle_signal(INJ, "test")
    rec = proxy.STATE.positions[-1]
    results.append((resp.get("ok") is True, f"señal INJ aceptada: {resp.get('error') or 'ok'}"))
    # riesgo 0.5% de 1000 = 5 USDT / (6.296 - 6.102) -> 25.77 INJ
    results.append((rec["qty"] == "25.77", f"sizing por riesgo -> qty {rec['qty']} (esperado 25.77)"))
    results.append((rec["status"] == "WAITING_FILL", "limit sin llenar con el mercado por debajo"))

    market.tick("INJUSDT", 6.05)
    proxy.monitor_once()
    results.append((proxy.STATE.positions[-1]["status"] == "WAITING_FILL", "sigue esperando a 6.05"))

    # mecha a 6.11 entre polls: el último precio vuelve a 6.08 pero la vela guarda el máximo
    market.tick("INJUSDT", 6.08, high=6.11)
    proxy.monitor_once()
    rec = proxy.STATE.positions[-1]
    results.append((rec["status"] == "OPEN", f"la mecha llena la entrada (status {rec['status']})"))
    pos = next(iter(ex.s["positions"].values()))
    results.append((pos["avgOpenPrice"] == "6.102", f"fill maker en el precio límite ({pos['avgOpenPrice']})"))
    results.append((pos["sl"] == "6.296", f"SL de posición en 6.296 ({pos['sl']})"))
    results.append((len(ex.s["tps"]) == 5, f"5 TP parciales ({len(ex.s['tps'])})"))

    # ---- persistencia: otro PaperExchange lee el mismo estado ---------------------------
    again = PaperExchange(proxy.CFG, market, Path(os.environ["PAPER_STATE_FILE"]),
                          Path(os.environ["PAPER_JOURNAL_FILE"]), clock=clock)
    results.append((len(again.s["positions"]) == 1 and len(again.s["tps"]) == 5,
                    "el estado sobrevive a un reinicio"))

    # TP1 (5.9967) tocado -> BE
    market.tick("INJUSDT", 5.995, new_candle=True)
    proxy.monitor_once()
    proxy.monitor_once()
    rec = proxy.STATE.positions[-1]
    results.append((len(ex.s["tps"]) == 4, f"TP1 ejecutado, quedan {len(ex.s['tps'])}"))
    results.append((rec.get("be_done") is True, "break-even tras TP1"))
    results.append((pos["positionId"] in ex.s["positions"]
                    and ex.s["positions"][pos["positionId"]]["sl"] == "6.0959",
                    "SL movido a 6.0959 (entrada - 0.1%)"))

    # vuelve arriba: salta el BE
    market.tick("INJUSDT", 6.10, new_candle=True)
    proxy.monitor_once()
    proxy.monitor_once()
    rec = proxy.STATE.positions[-1]
    results.append((rec["status"] == "CLOSED", f"cerrada por BE (status {rec['status']})"))
    rows = journal()
    row = rows[-1] if rows else {}
    results.append((row.get("close_reason") == "BE/SL", f"motivo en el journal: {row.get('close_reason')}"))
    results.append((row.get("pnl", -1) > 0 and 0 < (row.get("r") or 0) < 0.2,
                    f"TP1 + BE deja PnL {row.get('pnl')} USDT = {row.get('r')}R"))

    # ---- 2. BNB long: SL y TP en la misma vela -> manda el SL ----------------------------
    market.tick("BNBUSDT", 599.5)
    resp = proxy.handle_signal(BNB, "test")
    results.append((resp.get("ok") is True, "señal BNB aceptada (limit ejecutable al mercado)"))
    proxy.monitor_once()
    results.append((proxy.STATE.positions[-1]["status"] == "OPEN", "BNB abierta"))
    market.tick("BNBUSDT", 600, high=606, low=589, new_candle=True)
    proxy.monitor_once()
    proxy.monitor_once()
    row = journal()[-1]
    results.append((row["symbol"] == "BNBUSDT" and row["close_reason"] == "SL",
                    f"vela con TP1 y SL: se ejecuta el SL ({row['close_reason']})"))
    results.append((-1.15 < row["r"] < -0.95, f"pérdida ~1R con comisiones ({row['r']}R)"))
    entry_fill = row["fills"][0]
    results.append((entry_fill["price"] == 599.5 and entry_fill["fee"] > 0,
                    f"limit ejecutable: fill taker al mercado ({entry_fill['price']})"))

    # ---- 3. fill de la limit y SL dentro de la misma vela -> pierde ---------------------
    market.tick("BNBUSDT", 601, new_candle=True)
    resp = proxy.handle_signal(BNB, "test")
    results.append((resp.get("ok") is True and proxy.STATE.positions[-1]["status"] == "WAITING_FILL",
                    "segunda señal BNB esperando el fill"))
    market.tick("BNBUSDT", 595, low=588)
    proxy.monitor_once()
    proxy.monitor_once()
    row = journal()[-1]
    results.append((proxy.STATE.positions[-1]["status"] == "CLOSED" and row["close_reason"] == "SL"
                    and row["entry"] == 600, f"fill y SL en el mismo rango: SL ({row['close_reason']})"))

    equity, available = ex.equity()
    expected = 1000 + sum(r["pnl"] for r in journal())
    results.append((abs(equity - expected) < 1e-6 and abs(available - equity) < 1e-6,
                    f"equity {equity:.4f} = 1000 + PnL del journal ({expected:.4f})"))

    print()
    ok = True
    for passed, label in results:
        ok &= passed
        print(f"  {'OK  ' if passed else 'FAIL'}  {label}")
    print("\ntest_paper", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
