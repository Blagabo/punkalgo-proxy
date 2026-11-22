#!/usr/bin/env python3
"""
test_monitor.py — prueba el ciclo de vida completo del bot contra un exchange simulado.

No toca la red: se sustituye el cliente de Bitunix por un falso que simula el fill de la
orden de entrada, la posición, los TP/SL y el cierre.

    python3 test_monitor.py
"""

from __future__ import annotations

import os
import sys

os.environ.update({
    "DRY_RUN": "false",
    "STATE_FILE": "/tmp/opencode/proxy_test_state.json",
    "LOG_FILE": "/tmp/opencode/proxy_test.log",
    "WEBHOOK_TOKEN": "",
    "LOG_LEVEL": "DEBUG",
    "MAX_PRICE_DEVIATION_PCT": "0",
    "POSITION_SIZE_PCT": "10",
    "MAX_TOTAL_MARGIN_PCT": "50",
    "MAX_OPEN_POSITIONS": "10",
    "BITUNIX_API_KEY": "test", "BITUNIX_API_SECRET": "test",
})
if os.path.exists(os.environ["STATE_FILE"]):
    os.remove(os.environ["STATE_FILE"])

import proxy  # noqa: E402

SIGNAL = open(os.path.join(os.path.dirname(__file__), "signals", "sample.txt"), encoding="utf-8").read()

PAIR = {
    "symbol": "CHZUSDT", "basePrecision": 0, "quotePrecision": 5, "minTradeVolume": "88",
    "maxLeverage": 50, "symbolStatus": "OPEN", "isApiSupported": True,
    "maxLimitOrderVolume": "1000000000",
}


class FakeExchange:
    """Simula el trozo de Bitunix que usa el bot."""

    def __init__(self) -> None:
        self.position_mode = "ONE_WAY"
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.pos_tp_sl: dict[str, dict] = {}      # orden de posición (SL [+ TP])
        self.tp_orders: dict[str, dict] = {}      # TPs parciales
        self.calls: list[str] = []
        self.tick = 0
        self.price = 0.00884
        self.next_id = 1000
        self.setup: dict[str, int] = {}
        self.margin_mode = "ISOLATION"

    # ---- helpers ------------------------------------------------------------------
    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    # ---- mercado / cuenta ---------------------------------------------------------
    def pair(self, symbol: str) -> dict:
        return PAIR

    def trading_pairs(self, force: bool = False) -> dict:
        return {"CHZUSDT": PAIR}

    def last_price(self, symbol: str) -> float:
        return self.price

    def equity(self) -> tuple[float, float]:
        return 2000.0, 2000.0

    def account(self) -> dict:
        return {"available": "2000", "positionMode": self.position_mode}

    def ensure_symbol_setup(self, symbol: str, leverage: int) -> None:
        assert self.margin_mode == "ISOLATION", "el margen debe seguir aislado"
        self.setup[symbol] = leverage
        self.calls.append(f"setup:{symbol}:x{leverage}")

    # ---- lectura ------------------------------------------------------------------
    def order_detail(self, order_id: str | None = None, client_id: str | None = None) -> dict | None:
        order = self.orders.get(str(order_id))
        if not order:
            return None
        order = dict(order)
        if self.tick >= 1:
            order["status"], order["tradeQty"] = "FILLED", order["qty"]
        return order

    def pending_positions(self, symbol: str | None = None) -> list[dict]:
        return [dict(p) for p in self.positions.values()
                if float(p["qty"]) != 0 and (symbol is None or p["symbol"] == symbol)]

    def position(self, position_id: str) -> dict | None:
        pos = self.positions.get(str(position_id))
        return dict(pos) if pos and float(pos["qty"]) else None

    def pending_tp_sl(self, symbol: str | None = None, position_id: str | None = None) -> list[dict]:
        rows = [dict(r) for r in self.tp_orders.values() if not r.get("done")]
        if position_id:
            rows = [r for r in rows if r["positionId"] == str(position_id)]
        return rows

    # ---- escritura ----------------------------------------------------------------
    def place_order(self, symbol, side, qty, price, order_type, effect, client_id, trade_side=None):
        oid = self._id()
        self.orders[oid] = {"orderId": oid, "symbol": symbol, "side": side, "qty": qty,
                            "tradeQty": "0", "status": "NEW", "price": price}
        self.calls.append(f"order:{symbol}:{side}:{qty}@{price}")
        return {"orderId": oid, "clientId": client_id}

    def place_position_tp_sl(self, symbol, position_id, sl_price, tp_price=None):
        oid = self._id()
        self.pos_tp_sl[position_id] = {"orderId": oid, "slPrice": sl_price, "tpPrice": tp_price,
                                      "slStopType": proxy.CFG.sl_stop_type}
        self.calls.append(f"pos_tp_sl:{sl_price}:{tp_price}")
        return {"orderId": oid}

    def modify_position_tp_sl(self, symbol, position_id, sl_price):
        assert position_id in self.pos_tp_sl, "debe existir una orden de posición que modificar"
        self.pos_tp_sl[position_id]["slPrice"] = sl_price
        self.calls.append(f"modify_sl:{sl_price}")
        return {"orderId": self.pos_tp_sl[position_id]["orderId"]}

    def place_partial_tp(self, symbol, position_id, tp_price, qty):
        oid = self._id()
        self.tp_orders[oid] = {"id": oid, "positionId": str(position_id),
                               "symbol": symbol, "tpPrice": tp_price, "tpQty": qty}
        self.calls.append(f"tp:{tp_price}:{qty}")
        return {"orderId": oid}

    def cancel_tp_sl(self, symbol, order_id):
        if order_id in self.tp_orders:
            self.tp_orders[order_id]["done"] = True
        self.calls.append(f"cancel_tp:{order_id}")
        return {"orderId": order_id}

    # ---- guion de la simulación ---------------------------------------------------
    def advance(self) -> None:
        """Simula el paso del tiempo en el exchange."""
        self.tick += 1
        if self.tick == 1:                                  # la limit llena
            order = next(iter(self.orders.values()))
            self.positions["9001"] = {"positionId": "9001", "symbol": "CHZUSDT", "side": "SHORT",
                                      "qty": order["qty"], "avgOpenPrice": order["price"],
                                      "marginMode": "ISOLATION", "leverage": 20,
                                      "ctime": int(proxy.time.time() * 1000)}
        if self.tick == 3:                                  # toca Objetivo 1
            self.price = 0.00873
            for row in self.tp_orders.values():
                if row["tpPrice"] == "0.00873":
                    row["done"] = True
        if self.tick == 5:                                  # salta un TP final y cierra todo
            for pos in self.positions.values():
                pos["qty"] = "0"


def main() -> int:
    fake = FakeExchange()
    proxy.EX = fake
    proxy.STATE.positions = []
    proxy.STATE.seen = {}

    results: list[tuple[bool, str]] = []

    resp = proxy.handle_signal(SIGNAL, "test")
    results.append((resp.get("ok") is True, f"señal aceptada: {resp}"))
    results.append((fake.setup.get("CHZUSDT") == 20, "apalancamiento x20 aplicado al símbolo"))
    rec = proxy.STATE.positions[-1]
    results.append((rec["status"] == "WAITING_FILL", "registro en WAITING_FILL"))
    expected_qty = proxy.fmt_qty(2000 * proxy.CFG.position_size_pct / 100 * 20 / 0.00883, 0)
    results.append((rec["qty"] == expected_qty,
                    f"tamaño = {proxy.CFG.position_size_pct:g}% de 2000USDT a x20 -> "
                    f"{rec['qty']} (esperado {expected_qty})"))
    results.append((len(rec["tps"]) == 5, "5 objetivos guardados"))

    fake.advance()                       # tick 1: orden llena
    proxy.monitor_once()
    rec = proxy.STATE.positions[-1]
    results.append((rec["status"] == "OPEN", f"posición armada (status={rec['status']})"))
    results.append((bool(rec["sl_order_id"]), "SL de posición colocado"))
    results.append((len(rec["tp_orders"]) == 5, f"5 TPs parciales ({len(rec['tp_orders'])})"))
    results.append((fake.pos_tp_sl["9001"]["slPrice"] == "0.00901",
                    f"SL en 0.00901 (es {fake.pos_tp_sl['9001']['slPrice']})"))
    results.append((fake.pos_tp_sl["9001"]["tpPrice"] is None,
                    "la orden de posición lleva sólo el SL (TPs parciales aparte)"))
    tps_placed = sorted(float(p) for p in rec["tp_orders"])
    results.append((tps_placed == [0.00866, 0.00868, 0.0087, 0.00872, 0.00873],
                    f"TPs en los 5 objetivos: {tps_placed}"))
    sizes = [int(m["qty"]) for m in rec["tp_orders"].values()]
    results.append((sum(sizes) == int(rec["qty"]), f"los TPs suman la posición entera: {sizes}"))

    fake.advance()                       # tick 2: sin cambios
    proxy.monitor_once()
    results.append((not proxy.STATE.positions[-1]["be_done"], "aún sin break-even antes de TP1"))

    fake.advance()                       # tick 3: precio en Objetivo 1 y TP1 ejecutado
    proxy.monitor_once()
    rec = proxy.STATE.positions[-1]
    results.append((rec["be_done"] is True, "break-even aplicado al llegar a TP1"))
    results.append((fake.pos_tp_sl["9001"]["slPrice"] == "0.00882",
                    f"SL movido a 0.00882 (entrada + 0.1%) -> {fake.pos_tp_sl['9001']['slPrice']}"))
    results.append((rec["be_price"] == 0.00882, "be_price guardado en el registro"))
    results.append((len([r for r in fake.tp_orders.values() if not r.get("done")]) == 4,
                    "los otros 4 objetivos siguen vivos"))

    fake.advance()                       # tick 4: BE no se repite
    proxy.monitor_once()
    modifies = [c for c in fake.calls if c.startswith("modify_sl")]
    results.append((len(modifies) == 1, f"el BE se aplica una sola vez ({len(modifies)} modify)"))

    fake.advance()                       # tick 5: posición cerrada en el exchange
    proxy.monitor_once()
    rec = proxy.STATE.positions[-1]
    results.append((rec["status"] == "CLOSED", f"registro cerrado ({rec['note']})"))
    results.append((not [r for r in fake.tp_orders.values() if not r.get("done")],
                    "los TP huérfanos quedaron cancelados"))
    results.append((not proxy.STATE.active(), "no queda ninguna operación activa"))

    # --- tope de margen total (MAX_TOTAL_MARGIN_PCT) -------------------------------
    fake.tick = 1                     # congelamos la simulación: sólo probamos el gateo
    accepted = rejected = 0
    last_error = ""
    for letters in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
        out = proxy.handle_signal(SIGNAL.replace("#CHZ/USDT", f"#{letters}/USDT"), "test-cap")
        if out.get("ok"):
            accepted += 1
        else:
            rejected += 1
            last_error = str(out.get("error"))
    results.append((accepted == 5 and rejected == 1,
                    f"tope de margen: {accepted} aceptadas y {rejected} rechazada "
                    f"(tope {proxy.CFG.max_total_margin_pct:g}%, usado {proxy.committed_margin():.0f}USDT)"))
    results.append(("margen sumado" in last_error, f"el rechazo explica el tope: {last_error}"))

    print()
    for ok, label in results:
        print(("  OK    " if ok else "  FALLA ") + label)
    passed = all(ok for ok, _ in results)
    print("\n" + ("test_monitor OK" if passed else "test_monitor FALLÓ"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
