"""
paper.py — exchange de papel con precios reales de Bitunix.

Implementa el mismo trozo de interfaz que la clase `Bitunix` de proxy.py, así que todo el
ciclo del bot (fill, SL, TP parciales, break-even, cierre) corre sin cambios. Los datos de
mercado (pares, tick, precio, velas de 1 m) salen de la API pública de Bitunix; las órdenes,
posiciones y el saldo viven en local (`PAPER_STATE_FILE`).

Cómo casa las órdenes, en cada sincronización (como mucho una por segundo y símbolo):
  * Rango de precio desde la última vez: el último precio más el máximo/mínimo NUEVO de las
    velas de 1 m (si la vela en curso marca un máximo mayor que el que ya habíamos visto,
    ese máximo ocurrió desde la última consulta). Así no se pierden las mechas entre polls.
  * Entrada LIMIT: se llena al tocar el precio (maker). Si al colocarla ya es ejecutable,
    se llena en el acto al precio de mercado (taker). MARKET: precio de mercado + slippage.
  * Conservador: si en el mismo rango caben el SL y un TP, se ejecuta primero el SL.
  * SL y TP a mercado: se llenan en su precio con slippage en contra y comisión taker.

Cada operación cerrada se apunta en `PAPER_JOURNAL_FILE` (JSON por línea) con el PnL neto
de comisiones y el resultado en R sobre el SL original.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


class PaperExchange:
    """Exchange simulado sobre precios reales. Interfaz compatible con `proxy.Bitunix`."""

    def __init__(self, cfg: Any, market: Any, state_path: Path, journal_path: Path,
                 log: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 error_cls: type[Exception] = RuntimeError) -> None:
        self.cfg = cfg
        self.market = market                  # cliente Bitunix real: sólo endpoints públicos
        self.state_path = Path(state_path)
        self.journal_path = Path(journal_path)
        self.log = log or (lambda level, msg: None)
        self.clock = clock
        self.error_cls = error_cls            # proxy.BitunixError, para que el bot lo trate igual
        self.position_mode = "ONE_WAY"
        self.lock = threading.RLock()
        self._synced: dict[str, float] = {}   # última sincronización por símbolo (segundos)
        self.s: dict[str, Any] = {
            "start_equity": float(cfg.paper_equity), "realized": 0.0, "fees": 0.0,
            "next_id": 1, "orders": {}, "positions": {}, "tps": {}, "leverage": {}, "seen": {},
        }
        self._load()

    # ---- persistencia ---------------------------------------------------------------
    def _load(self) -> None:
        if self.state_path.exists():
            try:
                self.s.update(json.loads(self.state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                self.log("ERROR", f"paper: estado ilegible ({exc!r}); empiezo de cero")

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def _id(self, prefix: str) -> str:
        n = int(self.s["next_id"])
        self.s["next_id"] = n + 1
        return f"{prefix}{n}"

    def _ms(self) -> int:
        return int(self.clock() * 1000)

    # ---- mercado (público, real) ----------------------------------------------------
    def trading_pairs(self, force: bool = False) -> dict:
        return self.market.trading_pairs(force)

    def pair(self, symbol: str) -> dict:
        return self.market.pair(symbol)

    def last_price(self, symbol: str) -> float | None:
        return self.market.last_price(symbol)

    def _window(self, symbol: str) -> tuple[float, float, float] | None:
        """(último, mínimo, máximo) del precio desde la consulta anterior."""
        last = self.market.last_price(symbol)
        if not last:
            return None
        lo = hi = float(last)
        try:
            candles = self.market.klines(symbol, limit=3)
        except Exception as exc:                       # sin velas seguimos con el último precio
            self.log("DEBUG", f"paper: velas de {symbol} no disponibles ({exc!r})")
            candles = []
        candles = sorted(candles, key=lambda c: int(c["time"]))
        seen = self.s["seen"].get(symbol)
        if seen:
            for c in candles:
                t, h, l = int(c["time"]), float(c["high"]), float(c["low"])
                if t == seen["t"]:
                    if h > seen["h"]:
                        hi = max(hi, h)
                    if l < seen["l"]:
                        lo = min(lo, l)
                elif t > seen["t"]:
                    hi, lo = max(hi, h), min(lo, l)
        if candles:
            c = candles[-1]
            self.s["seen"][symbol] = {"t": int(c["time"]), "h": float(c["high"]), "l": float(c["low"])}
        return float(last), lo, hi

    # ---- cuenta -------------------------------------------------------------------------
    def _unrealized(self) -> float:
        total = 0.0
        for pos in self.s["positions"].values():
            mark = pos.get("mark") or pos["avgOpenPrice"]
            total += self._pnl(pos["side"], float(pos["avgOpenPrice"]), float(mark), float(pos["qty"]))
        return total

    def _margin_used(self) -> float:
        used = 0.0
        for pos in self.s["positions"].values():
            used += float(pos["avgOpenPrice"]) * float(pos["qty"]) / max(1, int(pos["leverage"]))
        for o in self.s["orders"].values():
            if o["status"] == "NEW":
                used += float(o["price"] or 0) * float(o["qty"]) / max(1, int(o["leverage"]))
        return used

    def account(self) -> dict:
        _, available = self.equity()
        return {"available": f"{available:.4f}", "positionMode": self.position_mode}

    def equity(self) -> tuple[float, float]:
        with self.lock:
            balance = self.s["start_equity"] + self.s["realized"]
            return balance + self._unrealized(), balance - self._margin_used()

    def ensure_symbol_setup(self, symbol: str, leverage: int) -> None:
        with self.lock:
            self.s["leverage"][symbol] = int(leverage)
            self._save()

    # ---- órdenes de entrada ---------------------------------------------------------------
    def place_order(self, symbol: str, side: str, qty: str, price: str | None, order_type: str,
                    effect: str, client_id: str, trade_side: str | None = None) -> dict:
        with self.lock:
            oid = self._id("PO")
            order = {"orderId": oid, "clientId": client_id, "symbol": symbol, "side": side,
                     "qty": qty, "tradeQty": "0", "price": price, "avgPrice": None,
                     "orderType": order_type, "status": "NEW", "ctime": self._ms(),
                     "leverage": self.s["leverage"].get(symbol, self.cfg.default_leverage)}
            self.s["orders"][oid] = order
            win = self._window(symbol)
            if win:
                last = win[0]
                marketable = (order_type == "MARKET"
                              or (side == "BUY" and last <= float(price))
                              or (side == "SELL" and last >= float(price)))
                if marketable:
                    self._fill_entry(order, self._slip(last, side), taker=True)
            self._save()
            self.log("PAPER", f"{symbol}: orden {order_type} {side} {qty} @ {price or 'MERCADO'} "
                              f"({order['status']})")
            return {"orderId": oid, "clientId": client_id}

    def cancel_order(self, symbol: str, order_id: str | None = None, client_id: str | None = None) -> Any:
        with self.lock:
            order = self._find_order(order_id, client_id)
            if order and order["status"] == "NEW":
                order["status"] = "CANCELED"
                self._save()
            return {"orderId": order["orderId"] if order else order_id}

    def _find_order(self, order_id: str | None, client_id: str | None) -> dict | None:
        if order_id and str(order_id) in self.s["orders"]:
            return self.s["orders"][str(order_id)]
        for o in self.s["orders"].values():
            if client_id and o["clientId"] == client_id:
                return o
        return None

    def order_detail(self, order_id: str | None = None, client_id: str | None = None) -> dict | None:
        with self.lock:
            order = self._find_order(order_id, client_id)
            if order:
                self.sync(order["symbol"])
                return dict(order)
            return None

    # ---- posiciones y TP/SL ------------------------------------------------------------
    def pending_positions(self, symbol: str | None = None) -> list[dict]:
        with self.lock:
            if symbol:
                self.sync(symbol)
            return [dict(p) for p in self.s["positions"].values()
                    if symbol is None or p["symbol"] == symbol]

    def position(self, position_id: str) -> dict | None:
        with self.lock:
            pos = self.s["positions"].get(str(position_id))
            if pos:
                self.sync(pos["symbol"])
            pos = self.s["positions"].get(str(position_id))
            return dict(pos) if pos else None

    def pending_tp_sl(self, symbol: str | None = None, position_id: str | None = None) -> list[dict]:
        with self.lock:
            if symbol:
                self.sync(symbol)
            return [dict(t) for t in self.s["tps"].values()
                    if (symbol is None or t["symbol"] == symbol)
                    and (position_id is None or t["positionId"] == str(position_id))]

    def place_position_tp_sl(self, symbol: str, position_id: str, sl_price: str,
                             tp_price: str | None = None) -> dict:
        with self.lock:
            pos = self.s["positions"].get(str(position_id))
            if not pos:
                self._missing(position_id)
            pos["sl"] = sl_price
            pos["tp"] = tp_price
            pos.setdefault("sl_initial", sl_price)
            pos.setdefault("sl_order_id", self._id("PSL"))
            self._check_now(pos)
            self._save()
            return {"orderId": pos["sl_order_id"]}

    def modify_position_tp_sl(self, symbol: str, position_id: str, sl_price: str) -> dict:
        with self.lock:
            pos = self.s["positions"].get(str(position_id))
            if not pos:
                self._missing(position_id)
            pos["sl"] = sl_price
            self._check_now(pos)
            self._save()
            return {"orderId": pos.get("sl_order_id")}

    def place_partial_tp(self, symbol: str, position_id: str, tp_price: str, qty: str) -> dict:
        with self.lock:
            if str(position_id) not in self.s["positions"]:
                self._missing(position_id)
            oid = self._id("PTP")
            self.s["tps"][oid] = {"id": oid, "orderId": oid, "positionId": str(position_id),
                                  "symbol": symbol, "tpPrice": tp_price, "tpQty": qty}
            self._save()
            return {"orderId": oid}

    def cancel_tp_sl(self, symbol: str, order_id: str) -> Any:
        with self.lock:
            self.s["tps"].pop(str(order_id), None)
            self._save()
            return {"orderId": order_id}

    def _missing(self, position_id: Any) -> None:
        raise self.error_cls("paper", f"posición {position_id} inexistente", "paper")

    # ---- motor de casado -------------------------------------------------------------------
    def sync(self, symbol: str, force: bool = False) -> None:
        """Avanza la simulación del símbolo con el rango de precio real desde la última vez."""
        now = self.clock()
        if not force and now - self._synced.get(symbol, 0.0) < 1.0:
            return
        self._synced[symbol] = now
        active = (any(o["symbol"] == symbol and o["status"] == "NEW" for o in self.s["orders"].values())
                  or any(p["symbol"] == symbol for p in self.s["positions"].values()))
        if not active:
            return
        win = self._window(symbol)
        if not win:
            return
        last, lo, hi = win

        for order in list(self.s["orders"].values()):
            if order["symbol"] != symbol or order["status"] != "NEW":
                continue
            px = float(order["price"])
            touched = lo <= px if order["side"] == "BUY" else hi >= px
            if touched:
                pid = self._fill_entry(order, px, taker=False)
                # el resto del rango pudo pasar después del fill: cuando llegue el SL se
                # comprueba contra él (sólo el lado adverso, nunca a favor)
                self.s["positions"][pid]["fill_window"] = [lo, hi]

        for pos in list(self.s["positions"].values()):
            if pos["symbol"] != symbol or pos.get("fill_window"):
                continue                       # recién llena: su rango ya se usó para el fill
            pos["mark"] = last
            self._check_range(pos, lo, hi)
        self._save()

    def _check_now(self, pos: dict) -> None:
        """Al colocar o mover el SL: si el precio ya lo ha cruzado, cierra al precio actual."""
        window = pos.pop("fill_window", None)
        if window and pos.get("sl"):
            sl = float(pos["sl"])
            adverse = window[1] if pos["side"] == "SHORT" else window[0]
            if adverse >= sl if pos["side"] == "SHORT" else adverse <= sl:
                self._close_qty(pos, float(pos["qty"]),
                                self._slip(sl, "BUY" if pos["side"] == "SHORT" else "SELL"), "SL")
                return
        last = self.market.last_price(pos["symbol"])
        if last:
            pos["mark"] = float(last)
            self._check_range(pos, float(last), float(last), gap=True)

    def _check_range(self, pos: dict, lo: float, hi: float, gap: bool = False) -> None:
        short = pos["side"] == "SHORT"
        adverse, favorable = (hi, lo) if short else (lo, hi)
        sl = float(pos["sl"]) if pos.get("sl") else None
        if sl is not None and (adverse >= sl if short else adverse <= sl):   # primero el SL
            exit_px = adverse if gap else sl
            self._close_qty(pos, float(pos["qty"]), self._slip(exit_px, "BUY" if short else "SELL"),
                            "SL" if pos.get("sl") == pos.get("sl_initial") else "BE/SL")
            return
        if pos.get("tp"):                                                     # TP único de posición
            tp = float(pos["tp"])
            if favorable <= tp if short else favorable >= tp:
                self._close_qty(pos, float(pos["qty"]), self._slip(tp, "BUY" if short else "SELL"), "TP")
                return
        rows = sorted((t for t in self.s["tps"].values() if t["positionId"] == pos["positionId"]),
                      key=lambda t: float(t["tpPrice"]), reverse=short)
        for tp in rows:
            price = float(tp["tpPrice"])
            if not (favorable <= price if short else favorable >= price):
                break
            self.s["tps"].pop(tp["id"], None)
            qty = min(float(tp["tpQty"]), float(pos["qty"]))
            self._close_qty(pos, qty, self._slip(price, "BUY" if short else "SELL"), f"TP {tp['tpPrice']}")
            if pos["positionId"] not in self.s["positions"]:
                return

    # ---- contabilidad ---------------------------------------------------------------------
    @staticmethod
    def _pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
        return (exit_px - entry) * qty * (-1.0 if side == "SHORT" else 1.0)

    def _slip(self, price: float, side: str) -> float:
        k = float(self.cfg.paper_slippage_pct) / 100.0
        return price * (1 + k) if side == "BUY" else price * (1 - k)

    def _fee(self, notional: float, taker: bool) -> float:
        pct = self.cfg.paper_taker_fee_pct if taker else self.cfg.paper_maker_fee_pct
        return notional * float(pct) / 100.0

    def _fill_entry(self, order: dict, price: float, taker: bool) -> str:
        qty = float(order["qty"])
        fee = self._fee(price * qty, taker)
        order.update(status="FILLED", tradeQty=order["qty"], avgPrice=f"{price:.10g}")
        pid = self._id("PP")
        side = "LONG" if order["side"] == "BUY" else "SHORT"
        self.s["positions"][pid] = {
            "positionId": pid, "symbol": order["symbol"], "side": side, "qty": order["qty"],
            "qty_open": order["qty"], "avgOpenPrice": f"{price:.10g}", "leverage": order["leverage"],
            "marginMode": "ISOLATION", "ctime": self._ms(), "mark": price, "sl": None, "tp": None,
            "fees": fee, "realized": 0.0, "fills": [{"kind": "ENTRY", "price": price, "qty": qty,
                                                     "fee": fee, "ts": self._ms()}],
            "signal_entry": order["price"], "client_id": order["clientId"],
        }
        self.s["realized"] -= fee
        self.s["fees"] += fee
        self.log("PAPER", f"{order['symbol']}: entrada {side} {order['qty']} llena @ {price:.10g} "
                          f"({'taker' if taker else 'maker'}, fee {fee:.4f})")
        return pid

    def _close_qty(self, pos: dict, qty: float, price: float, reason: str) -> None:
        qty = min(qty, float(pos["qty"]))
        if qty <= 0:
            return
        gross = self._pnl(pos["side"], float(pos["avgOpenPrice"]), price, qty)
        fee = self._fee(price * qty, taker=True)
        pos["realized"] += gross - fee
        pos["fees"] += fee
        pos["fills"].append({"kind": reason, "price": price, "qty": qty, "fee": fee, "ts": self._ms()})
        self.s["realized"] += gross - fee
        self.s["fees"] += fee
        left = float(pos["qty"]) - qty
        pos["qty"] = f"{left:.10g}" if left > 1e-12 else "0"
        self.log("PAPER", f"{pos['symbol']}: {reason} {qty:g} @ {price:.10g} -> PnL {gross - fee:+.4f} USDT")
        if float(pos["qty"]) <= 0:
            self._finish(pos, reason)

    def _finish(self, pos: dict, reason: str) -> None:
        self.s["positions"].pop(pos["positionId"], None)
        for tid in [k for k, t in self.s["tps"].items() if t["positionId"] == pos["positionId"]]:
            self.s["tps"].pop(tid, None)
        entry = float(pos["avgOpenPrice"])
        sl0 = float(pos.get("sl_initial") or 0)
        qty0 = float(pos["qty_open"])
        risk = abs(entry - sl0) * qty0 if sl0 else 0.0
        entry_fee = pos["fills"][0]["fee"]
        row = {
            "position_id": pos["positionId"], "client_id": pos.get("client_id"),
            "symbol": pos["symbol"], "side": pos["side"], "leverage": pos["leverage"],
            "signal_entry": pos.get("signal_entry"), "entry": entry, "qty": qty0,
            "sl_initial": sl0 or None, "opened_ms": pos["ctime"], "closed_ms": self._ms(),
            "close_reason": reason, "fills": pos["fills"], "fees": round(pos["fees"], 6),
            "pnl": round(pos["realized"] - entry_fee, 6),
            "r": round((pos["realized"] - entry_fee) / risk, 4) if risk else None,
        }
        # pos["realized"] no incluye la comisión de entrada; el PnL neto del journal sí
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.log("PAPER", f"{pos['symbol']}: cerrada ({reason}) PnL {row['pnl']:+.4f} USDT"
                          + (f" = {row['r']:+.2f}R" if row["r"] is not None else ""))

