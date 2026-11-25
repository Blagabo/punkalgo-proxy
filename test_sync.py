#!/usr/bin/env python3
"""test_sync.py — filas que sync.py manda a Supabase, sin red.  python3 test_sync.py"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="sync_test_"))
os.environ.update({
    "PROXY_ENV_FILE": str(TMP / "none"), "SUPABASE_URL": "https://x.supabase.co",
    "SUPABASE_SECRET_KEY": "sb_secret_test", "BOT_OWNER_ID": "00000000-0000-0000-0000-000000000001",
    "STATE_FILE": str(TMP / "state.json"), "PAPER_STATE_FILE": str(TMP / "paper.json"),
    "PAPER_JOURNAL_FILE": str(TMP / "journal.jsonl"), "MESSAGES_FILE": str(TMP / "msgs.jsonl"),
    "SYNC_CACHE_FILE": str(TMP / "cache.json"),
    "PAPER_OUTCOMES_FILE": str(TMP / "outcomes.jsonl"), "LISTENER_HEARTBEAT_FILE": str(TMP / "hb.json"),
    "SYNC_LOG_FILE": str(TMP / "sync.log"),
})

import sync  # noqa: E402

SIGNAL = "#NEAR/USDT - Short 🔴\n\nEntrada: 2.886\nStop Loss: 2.946\n\n🎯 Objetivo 1: 2.836\n\nApalancamiento: x20"
ORDI = "#ORDI/USDT \n\nStop Loss Alcanzado ⛔\n\nPérdida: -66.1903% 📉"

(TMP / "msgs.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
    {"ts": 1, "msg_id": 742, "date": "2026-11-24T12:35:50+00:00", "edited": False, "type": "signal",
     "text": SIGNAL, "forwarded": True, "proxy": {"ok": True, "order_id": "PO1"}, "delay_s": 1.4, "source": "push"},
    {"ts": 2, "msg_id": 742, "date": "2026-11-24T12:35:50+00:00", "edited": True, "type": "signal",
     "text": SIGNAL, "forwarded": False, "skip": "ya enviada"},
    {"ts": 3, "msg_id": 743, "date": "2026-11-24T12:40:06+00:00", "edited": False, "type": "other",
     "text": ORDI, "forwarded": False},
    {"ts": 4, "msg_id": 744, "date": "2026-11-24T12:41:00+00:00", "edited": False, "type": "signal",
     "text": SIGNAL.replace("NEAR", "APT"), "forwarded": True, "proxy": {"ok": False, "error": "plan inválido"}},
]) + "\n", encoding="utf-8")

(TMP / "state.json").write_text(json.dumps({"positions": [
    {"id": "pxA", "symbol": "NEARUSDT", "side": "SHORT", "status": "CLOSED", "entry_price": 2.886, "sl": 2.946,
     "tps": [2.836], "qty": "83", "leverage": 20, "notional": 240.5, "order_id": "PO1", "created": 1795000000,
     "be_done": True, "note": ""},
    {"id": "pxB", "symbol": "APTUSDT", "side": "SHORT", "status": "OPEN", "entry_price": 0.3736, "sl": 0.3797,
     "tps": [0.3672], "qty": "819", "leverage": 20, "notional": 306, "order_id": "PO9", "created": 1795000300},
    {"id": "pxC", "symbol": "ORDIUSDT", "side": "LONG", "status": "CLOSED", "entry_price": 5, "sl": 4.8,
     "tps": [5.2], "qty": "10", "leverage": 20, "order_id": "PO20", "created": 1795000600,
     "note": "entrada sin fill, cancelada por TTL de 900s"},
    {"id": "pxD", "symbol": "XUSDT", "side": "LONG", "status": "DRY_RUN", "created": 1795000700},
    {"id": "pxE", "symbol": "SOLUSDT", "side": "LONG", "status": "WAITING_FILL", "entry_price": 182.4, "sl": 178.1,
     "tps": [190], "qty": "1.1", "leverage": 20, "order_id": "PO30", "created": 1795000800},
]}), encoding="utf-8")
(TMP / "paper.json").write_text(json.dumps({"start_equity": 1000, "positions": {"PP10": {
    "client_id": "pxB", "side": "SHORT", "avgOpenPrice": "0.37362526", "ctime": 1795000300000, "fees": 0.18,
    "qty": "655.2", "qty_open": "819", "sl": "0.3736", "sl_initial": "0.3797", "mark": 0.37, "realized": 1.0,
    "best": 0.3655, "worst": 0.3760,
    "fills": [{"kind": "ENTRY", "price": 0.3736, "qty": 819, "fee": 0.18},
              {"kind": "TP 0.3672", "price": 0.3672, "qty": 163.8}]}},
    "orders": {"PO30": {"clientId": "pxE", "status": "NEW", "mark": 182.0}}}), encoding="utf-8")
(TMP / "journal.jsonl").write_text(json.dumps({
    "client_id": "pxA", "symbol": "NEARUSDT", "side": "SHORT", "leverage": 20, "signal_entry": "2.886",
    "entry": 2.8864, "qty": 83, "sl_initial": 2.946, "opened_ms": 1795000001000, "closed_ms": 1795003600000,
    "close_reason": "BE/SL", "fills": [{"kind": "ENTRY"}, {"kind": "TP 2.836"}, {"kind": "BE/SL"}],
    "fees": 0.3, "pnl": 1.2, "r": 0.24, "best": 2.8268, "worst": 2.9162}) + "\n", encoding="utf-8")

(TMP / "outcomes.jsonl").write_text("\n".join(json.dumps(r) for r in [
    {"client_id": "pxA", "state": "tracking", "reach_tp": 1},
    {"client_id": "pxA", "state": "sl", "reach_tp": 3},
]) + "\n", encoding="utf-8")
(TMP / "hb.json").write_text(json.dumps({"ts": 1795000000, "connected": True, "polled": 2, "late": 1}), encoding="utf-8")


def main() -> int:
    sent: dict[str, list[dict]] = {}
    sync.upsert = lambda table, rows, conflict: sent.setdefault(table, []).extend(rows)
    s = sync.Sync()
    s.status = lambda: {"equity": 999.5, "available": 990.0, "config": {"broker": "paper"},
                        "counters": {}, "managed": [{}]}
    s.once()
    msgs = {m["msg_id"]: m for m in sent["bot_messages"]}
    trades = {t["id"]: t for t in sent["bot_trades"]}
    checks = [
        (len(msgs) == 3, f"3 mensajes (la edición se funde): {sorted(msgs)}"),
        (msgs[742]["edited"] and msgs[742]["outcome"] == "operada" and msgs[742]["trade_id"] == "pxA",
         f"742 operada y enlazada a pxA: {msgs[742]['outcome']} {msgs[742]['trade_id']}"),
        (msgs[743]["type"] == "sl_hit" and msgs[743]["info"]["loss_pct"] == -66.1903,
         f"743 reclasificado como sl_hit: {msgs[743]['type']}"),
        (msgs[744]["outcome"].startswith("rechazada"), f"744 rechazada: {msgs[744]['outcome']}"),
        (set(trades) == {"pxA", "pxB", "pxC", "pxE"}, f"DRY_RUN fuera: {sorted(trades)}"),
        (trades["pxA"]["status"] == "closed" and trades["pxA"]["r"] == 0.24 and trades["pxA"]["tps_hit"] == 1
         and trades["pxA"]["msg_id"] == 742 and trades["pxA"]["be_done"], "pxA cerrada con R, TP1, BE y mensaje"),
        (trades["pxB"]["status"] == "open" and trades["pxB"]["tps_hit"] == 1
         and abs(trades["pxB"]["fill_price"] - 0.37362526) < 1e-9, "pxB abierta con fill y 1 TP"),
        (trades["pxC"]["status"] == "expired", f"pxC caducada por TTL: {trades['pxC']['status']}"),
        # short 819 @ 0.37362526, SL 0.3797: riesgo 4.9752; quedan 655.2 a 0.37 y 1.0 cobrado - 0.18 de fee
        (abs(trades["pxB"]["risk"] - 0.00607474 * 819) < 1e-5 and abs(trades["pxB"]["upnl"] - (1.0 - 0.18 + 0.00362526 * 655.2)) < 1e-5
         and abs(trades["pxB"]["r_now"] - trades["pxB"]["upnl"] / trades["pxB"]["risk"]) < 1e-3
         and trades["pxB"]["mark"] == 0.37 and trades["pxB"]["sl_now"] == "0.3736",
         f"pxB en vivo: precio, SL vigente, PnL {trades['pxB']['upnl']} y {trades['pxB']['r_now']}R"),
        (trades["pxE"]["status"] == "waiting" and trades["pxE"]["mark"] == 182.0, "pxE esperando con su precio actual"),
        (abs(trades["pxA"]["risk"] - 4.9468) < 1e-3, f"riesgo de la cerrada: {trades['pxA']['risk']}"),
        (sent["bot_status"][0]["equity"] == 999.5 and sent["bot_status"][0]["start_equity"] == 1000
         and sent["bot_status"][0]["active"] == 1, "bot_status con equity, inicial y activas"),
        (len(sent.get("bot_equity", [])) == 1, "primera muestra de equity"),
        (msgs[742]["delay_s"] == 1.4 and msgs[742]["source"] == "push", "mensaje con retraso y origen"),
        # short a 2.8864 con SL 2.946 (riesgo 0.0596): mejor 2.8268 = +1R, peor 2.9162 = -0.5R
        (abs(trades["pxA"]["mfe_r"] - 1.0) < 1e-3 and abs(trades["pxA"]["mae_r"] + 0.5) < 1e-3,
         f"MFE/MAE en R de la cerrada: {trades['pxA']['mfe_r']} / {trades['pxA']['mae_r']}"),
        (trades["pxA"]["reach_tp"] == 3 and trades["pxA"]["reach_state"] == "sl",
         "seguimiento en sombra: la última línea manda (3 objetivos, terminó en SL)"),
        (trades["pxB"]["mfe_r"] is not None, f"MFE en vivo de la abierta: {trades['pxB']['mfe_r']}"),
        (sent["bot_status"][0]["config"]["listener"]["polled"] == 2, "latido del listener en bot_status"),
    ]
    sent.clear()
    s.once()
    checks.append((not sent.get("bot_messages") and not sent.get("bot_trades") and not sent.get("bot_equity"),
                   "segunda pasada sin cambios: no reenvía mensajes, operaciones ni equity"))
    checks.append((all(set(t) == set(sync.TRADE_COLS) for t in trades.values()),
                   "todas las operaciones con las mismas columnas (lote válido para PostgREST)"))

    # el proxy poda pxA de su estado: no se pierden ni campos ni el enlace con el mensaje
    state = json.loads((TMP / "state.json").read_text(encoding="utf-8"))
    state["positions"] = [p for p in state["positions"] if p["id"] != "pxA"]
    (TMP / "state.json").write_text(json.dumps(state), encoding="utf-8")
    sent.clear()
    s2 = sync.Sync()                    # además, tras un reinicio (caché en disco)
    s2.status = s.status
    s2.once()
    a = {t["id"]: t for t in sent["bot_trades"]}["pxA"]
    m = {x["msg_id"]: x for x in sent["bot_messages"]}[742]
    checks.append((a["tps"] == [2.836] and a["notional"] == 240.5 and a["msg_id"] == 742 and a["be_done"]
                   and m["trade_id"] == "pxA", "podada del estado y tras reinicio: conserva TPs, notional, BE y enlace"))

    # órdenes de la web: requests falso (Supabase + proxy)
    class Resp:
        def __init__(self, code, data):
            self.status_code, self._data, self.text, self.ok = code, data, json.dumps(data), code < 300
        def json(self):
            return self._data

    import time as _t
    now = _t.time()
    calls = []
    cmds = [{"id": "c1", "action": "close", "target": "pxB", "created_at": sync.iso(now - 5)},
            {"id": "c3", "action": "set_guards", "target": None, "params": {"daily_loss_limit_pct": 2, "max_open_risk_pct": 0},
             "created_at": sync.iso(now - 2)},
            {"id": "c2", "action": "close_all", "target": None, "created_at": sync.iso(now - 3600)}]

    class FakeRequests:
        RequestException = Exception
        @staticmethod
        def get(url, **kw):
            calls.append(("get", url, kw.get("params")))
            return Resp(200, cmds)
        @staticmethod
        def post(url, **kw):
            calls.append(("post", url, kw.get("json"), kw.get("headers")))
            if url.endswith("/api/guards"):
                return Resp(200, {"ok": True, "daily_loss_limit_pct": 2, "max_open_risk_pct": 0})
            return Resp(200, {"ok": True, "done": [{"symbol": "APTUSDT", "action": "closed"}], "failed": []})
        @staticmethod
        def patch(url, **kw):
            calls.append(("patch", kw["params"]["id"].removeprefix("eq."), json.loads(kw["data"])))
            return Resp(204, {})

    real = sync.requests
    sync.requests = FakeRequests
    try:
        n = sync.commands()
    finally:
        sync.requests = real
    posts = [c for c in calls if c[0] == "post"]
    patches = {c[1]: c[2] for c in calls if c[0] == "patch"}
    guard_posts = [c for c in posts if c[1].endswith("/api/guards")]
    checks.append((len(guard_posts) == 1 and guard_posts[0][2] == {"daily_loss_limit_pct": 2, "max_open_risk_pct": 0, "origin": "web"}
                   and patches.get("c3", {}).get("result") == "freno diario 2% · riesgo abierto desactivado",
                   f"orden set_guards: {patches.get('c3', {}).get('result')}"))
    posts = [c for c in posts if not c[1].endswith("/api/guards")]
    checks.append((n == 3 and len(posts) == 1 and posts[0][1].endswith("/api/close") and posts[0][2]["id"] == "pxB"
                   and posts[0][3]["X-Webhook-Token"] == sync.TOKEN, "orden 'close' pasada al proxy con id y token"))
    checks.append((patches.get("c1", {}).get("status") == "done" and "APTUSDT cerrada" in patches["c1"]["result"],
                   f"resultado apuntado: {patches.get('c1', {}).get('result')}"))
    checks.append((patches.get("c2", {}).get("status") == "failed" and "caducada" in patches["c2"]["result"],
                   "orden de hace 1 h descartada como caducada, sin tocar el proxy"))

    ok = True
    for passed, label in checks:
        ok &= passed
        print(f"  {'OK  ' if passed else 'FAIL'}  {label}")
    print("\ntest_sync", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
