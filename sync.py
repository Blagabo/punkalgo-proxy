#!/usr/bin/env python3
"""
sync.py — copia el estado del bot a Supabase para verlo en la web (TrackerProps → Bots).

Solo lee: el `/status` del proxy y los archivos de /data (estado del proxy, estado y journal
del paper, mensajes del listener). Escribe en las tablas `bot_status`, `bot_equity`,
`bot_trades` y `bot_messages` (migración 0005_bots.sql de TrackerProps) con la clave secreta
de Supabase, que nunca sale del VPS. Solo envía las filas que han cambiado.

    python sync.py          # bucle (servicio `sync` del compose)
    python sync.py once     # una pasada y sale

Además ejecuta las órdenes que la web deja en `bot_commands` (cerrar una operación o todas):
las mira cada COMMAND_POLL_SEC, las pasa al proxy (/api/close, /api/close-all) y apunta el
resultado. Una orden con más de COMMAND_MAX_AGE_SEC sin ejecutar se descarta.

Variables: SUPABASE_URL, SUPABASE_SECRET_KEY, BOT_OWNER_ID (tu user id de Supabase),
BOT_NAME, SYNC_INTERVAL_SEC, EQUITY_EVERY_SEC, COMMAND_POLL_SEC, COMMAND_MAX_AGE_SEC,
PROXY_STATUS_URL, WEBHOOK_TOKEN y las rutas STATE_FILE, PAPER_STATE_FILE,
PAPER_JOURNAL_FILE, MESSAGES_FILE.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def local(name: str, default: str) -> Path:
    p = Path(env(name, default))
    return p if p.is_absolute() else HERE / p


URL = env("SUPABASE_URL").rstrip("/")
KEY = env("SUPABASE_SECRET_KEY")
OWNER = env("BOT_OWNER_ID")
BOT = env("BOT_NAME", "punkchainer")
INTERVAL = float(env("SYNC_INTERVAL_SEC", "30"))
EQUITY_EVERY = float(env("EQUITY_EVERY_SEC", "300"))
COMMAND_POLL = float(env("COMMAND_POLL_SEC", "5"))
COMMAND_MAX_AGE = float(env("COMMAND_MAX_AGE_SEC", "600"))
STATUS_URL = env("PROXY_STATUS_URL",
                 f"http://127.0.0.1:{env('PROXY_PORT', '8080')}/status")
TOKEN = env("WEBHOOK_TOKEN")
STATE = local("STATE_FILE", "proxy_state.json")
PAPER_STATE = local("PAPER_STATE_FILE", "paper_state.json")
JOURNAL = local("PAPER_JOURNAL_FILE", "paper_trades.jsonl")
MESSAGES = local("MESSAGES_FILE", "tg_messages.jsonl")
CACHE = local("SYNC_CACHE_FILE", "sync_cache.json")

TRADE_COLS = ("user_id", "bot", "id", "msg_id", "symbol", "side", "status", "signal_at", "entry", "sl", "tps",
              "leverage", "qty", "notional", "fill_price", "opened_at", "closed_at", "close_reason", "be_done",
              "tps_hit", "fees", "pnl", "r", "fills", "note", "mark", "sl_now", "upnl", "r_now", "risk")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} sync {msg}", flush=True)


def iso(ts: float | int | None, ms: bool = False) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts) / (1000 if ms else 1), timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue                         # línea a medio escribir: entra en la próxima pasada
    return rows


# ---- construcción de filas -----------------------------------------------------------------

def tp_hits(fills: Iterable[dict]) -> int:
    return sum(1 for f in fills or [] if str(f.get("kind", "")).startswith("TP"))


def build_messages(rows: list[dict], order_to_trade: dict[str, str]) -> list[dict]:
    """Un registro por mensaje (las ediciones se funden), reclasificado con classify.py actual."""
    by_id: dict[int, dict] = {}
    for row in rows:
        mid = int(row.get("msg_id") or 0)
        if not mid:
            continue
        prev = by_id.get(mid)
        merged = {**(prev or {}), **row}
        merged["edited"] = bool(row.get("edited") or (prev or {}).get("edited"))
        if prev and prev.get("forwarded") and not row.get("forwarded"):
            merged.update(forwarded=True, proxy=prev.get("proxy"))
        by_id[mid] = merged

    out = []
    for mid, row in by_id.items():
        text = row.get("text") or ""
        info = classify(text)
        kind, symbol = info.pop("type"), info.pop("symbol", None)
        trade_id, outcome = None, None
        if kind == "signal":
            proxy = row.get("proxy") or {}
            if row.get("forwarded") and proxy.get("ok"):
                trade_id = order_to_trade.get(str(proxy.get("order_id") or ""))
                outcome = "operada"
            elif row.get("forwarded"):
                outcome = f"rechazada: {proxy.get('error') or 'sin respuesta'}"
            else:
                outcome = f"no enviada: {row.get('skip') or 'desconocido'}"
        out.append({
            "user_id": OWNER, "bot": BOT, "msg_id": mid,
            "posted_at": row.get("date") or iso(row.get("ts")),
            "edited": bool(row.get("edited")), "type": kind,
            "symbol": symbol, "info": info or None,
            "outcome": outcome, "trade_id": trade_id, "text": text[:4000],
        })
    return out


def trade_status(rec: dict, closed: bool) -> str | None:
    status = rec.get("status")
    if status == "WAITING_FILL":
        return "waiting"
    if status == "OPEN":
        return "open"
    if status == "CLOSED":
        if closed:
            return "closed"
        note = str(rec.get("note") or "").lower()
        return "expired" if ("ttl" in note or "sin fill" in note) else "cancelled"
    if status == "CANCELLED":                 # entrada cancelada a mano (/api/cancel o /api/close)
        return "cancelled"
    return None                               # DRY_RUN y similares no se publican


def build_trades(state: dict, paper: dict, journal: list[dict],
                 msg_of_trade: dict[str, int]) -> list[dict]:
    closed = {str(j.get("client_id")): j for j in journal if j.get("client_id")}
    live = {str(p.get("client_id")): p for p in (paper.get("positions") or {}).values() if p.get("client_id")}
    waiting = {str(o.get("clientId")): o for o in (paper.get("orders") or {}).values()
               if o.get("clientId") and o.get("status") == "NEW"}
    rows: dict[str, dict] = {}

    for cid, j in closed.items():             # cerradas: el journal manda
        fills = j.get("fills") or []
        rows[cid] = {
            "id": cid, "symbol": j["symbol"], "side": j["side"], "status": "closed",
            "signal_at": iso(j.get("opened_ms"), ms=True), "entry": j.get("signal_entry"),
            "sl": j.get("sl_initial"), "leverage": j.get("leverage"), "qty": j.get("qty"),
            "fill_price": j.get("entry"), "opened_at": iso(j.get("opened_ms"), ms=True),
            "closed_at": iso(j.get("closed_ms"), ms=True), "close_reason": j.get("close_reason"),
            "tps_hit": tp_hits(fills), "fees": j.get("fees"), "pnl": j.get("pnl"), "r": j.get("r"),
            "fills": fills, "risk": risk_of(j.get("entry"), j.get("sl_initial"), j.get("qty")),
        }

    for rec in state.get("positions") or []:  # el estado del proxy añade la señal y lo vivo
        cid = str(rec.get("id"))
        status = trade_status(rec, cid in closed)
        if not status:
            continue
        row = rows.setdefault(cid, {"id": cid})
        pos = live.get(cid)
        row.update({
            "symbol": rec["symbol"], "side": rec["side"], "status": status,
            "signal_at": iso(rec.get("created")), "entry": rec.get("entry_price"), "sl": rec.get("sl"),
            "tps": rec.get("tps"), "leverage": rec.get("leverage"), "qty": rec.get("qty"),
            "notional": rec.get("notional"), "be_done": bool(rec.get("be_done")),
            "note": rec.get("note") or None,
        })
        if pos:                               # abierta en el paper: fills, TPs y PnL hasta ahora
            row.update(live_fields(pos))
        elif status == "waiting" and cid in waiting:
            row["mark"] = waiting[cid].get("mark")

    for cid, row in rows.items():
        row.update(user_id=OWNER, bot=BOT, msg_id=msg_of_trade.get(cid))
    return [r for r in rows.values() if r.get("signal_at")]


def risk_of(entry: object, sl: object, qty: object) -> float | None:
    try:
        return round(abs(float(entry) - float(sl)) * float(qty), 6) if entry and sl and qty else None
    except (TypeError, ValueError):
        return None


def live_fields(pos: dict) -> dict:
    """Posición abierta del paper: precio actual, SL vigente y PnL/R flotantes (sin la comisión
    de salida). El PnL incluye los TP parciales ya cobrados y la comisión de entrada."""
    fills = pos.get("fills") or []
    entry = float(pos["avgOpenPrice"])
    mark = float(pos.get("mark") or entry)
    sign = -1.0 if pos.get("side") == "SHORT" else 1.0
    entry_fee = float(fills[0].get("fee") or 0) if fills else 0.0
    upnl = float(pos.get("realized") or 0) - entry_fee + (mark - entry) * sign * float(pos.get("qty") or 0)
    risk = risk_of(entry, pos.get("sl_initial"), pos.get("qty_open"))
    return {"fill_price": entry, "opened_at": iso(pos.get("ctime"), ms=True), "tps_hit": tp_hits(fills),
            "fills": fills, "fees": pos.get("fees"), "mark": mark, "sl_now": pos.get("sl"),
            "upnl": round(upnl, 6), "risk": risk, "r_now": round(upnl / risk, 4) if risk else None}


# ---- Supabase --------------------------------------------------------------------------------

def headers() -> dict[str, str]:
    h = {"apikey": KEY, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    if KEY.startswith("eyJ"):                 # clave service_role antigua (JWT)
        h["Authorization"] = f"Bearer {KEY}"
    return h


def upsert(table: str, rows: list[dict], conflict: str) -> None:
    for i in range(0, len(rows), 200):
        resp = requests.post(f"{URL}/rest/v1/{table}", params={"on_conflict": conflict},
                             headers=headers(), data=json.dumps(rows[i:i + 200], default=str), timeout=20)
        if resp.status_code >= 300:
            raise RuntimeError(f"{table}: HTTP {resp.status_code} {resp.text[:300]}")


class Sync:
    """El proxy solo conserva sus últimos 60 registros: la caché (en /data) guarda lo ya visto
    de cada operación y el enlace orden -> operación, para no perder campos al podarse."""

    def __init__(self) -> None:
        self.sent: dict[str, str] = {}        # clave de fila -> hash de lo último enviado
        self.last_equity = 0.0
        cache = read_json(CACHE)
        self.links: dict[str, str] = cache.get("links") or {}
        self.trades: dict[str, dict] = cache.get("trades") or {}

    def save_cache(self) -> None:
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"links": self.links, "trades": self.trades}, default=str), encoding="utf-8")
        tmp.replace(CACHE)

    def merge_trades(self, rows: list[dict]) -> list[dict]:
        out = []
        for row in rows:
            known = self.trades.get(row["id"], {})
            full = {**known, **{k: v for k, v in row.items() if v is not None}}
            self.trades[row["id"]] = full
            out.append({**{c: full.get(c) for c in TRADE_COLS},
                        "be_done": bool(full.get("be_done")), "tps_hit": int(full.get("tps_hit") or 0)})
        return out

    def changed(self, table: str, rows: list[dict], key: tuple[str, ...]) -> list[dict]:
        out = []
        for row in rows:
            k = table + "|" + "|".join(str(row[c]) for c in key)
            h = hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
            if self.sent.get(k) != h:
                out.append((k, h, row))
        return out

    def push(self, table: str, rows: list[dict], key: tuple[str, ...]) -> int:
        pending = self.changed(table, rows, key)
        if pending:
            upsert(table, [r for _, _, r in pending], ",".join(("user_id", "bot") + key))
            for k, h, _ in pending:
                self.sent[k] = h
        return len(pending)

    def status(self) -> dict | None:
        try:
            resp = requests.get(STATUS_URL, headers={"X-Webhook-Token": TOKEN}, timeout=10)
            return resp.json() if resp.ok else None
        except (requests.RequestException, ValueError):
            return None

    def once(self) -> str:
        state, paper = read_json(STATE), read_json(PAPER_STATE)
        self.links.update({str(r.get("order_id")): str(r.get("id"))
                           for r in state.get("positions") or [] if r.get("order_id")})
        messages = build_messages(read_jsonl(MESSAGES), self.links)
        msg_of_trade = {m["trade_id"]: m["msg_id"] for m in messages if m.get("trade_id")}
        trades = self.merge_trades(build_trades(state, paper, read_jsonl(JOURNAL), msg_of_trade))
        self.save_cache()

        n_msg = self.push("bot_messages", messages, ("msg_id",))
        n_trd = self.push("bot_trades", trades, ("id",))

        st = self.status()
        now = time.time()
        if st:
            cfg = st.get("config") or {}
            upsert("bot_status", [{
                "user_id": OWNER, "bot": BOT, "updated_at": iso(now), "broker": cfg.get("broker"),
                "equity": st.get("equity"), "available": st.get("available"),
                "start_equity": paper.get("start_equity"), "active": len(st.get("managed") or []),
                "config": {**cfg, "guards": st.get("guards")}, "counters": st.get("counters"),
            }], "user_id,bot")
            if st.get("equity") is not None and now - self.last_equity >= EQUITY_EVERY:
                upsert("bot_equity", [{"user_id": OWNER, "bot": BOT, "ts": iso(int(now // 60 * 60)),
                                       "equity": st["equity"]}], "user_id,bot,ts")
                self.last_equity = now
        return f"mensajes {n_msg} · operaciones {n_trd} · proxy {'ok' if st else 'sin respuesta'}"


def proxy_base() -> str:
    return STATUS_URL.rsplit("/status", 1)[0]


def run_command(cmd: dict) -> tuple[str, str]:
    """Pasa una orden de la web al proxy. Devuelve (estado, resultado legible)."""
    action, target = cmd.get("action"), (cmd.get("target") or "").strip()
    params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
    if action == "set_guards":
        body = {k: params[k] for k in ("daily_loss_limit_pct", "max_open_risk_pct") if k in params}
        try:
            resp = requests.post(f"{proxy_base()}/api/guards", json={**body, "origin": "web"},
                                 headers={"X-Webhook-Token": TOKEN}, timeout=30)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            return "failed", f"proxy sin respuesta: {exc}"
        if not data.get("ok"):
            return "failed", str(data.get("error") or "rechazado")[:500]
        txt = lambda v: f"{v:g}%" if v else "desactivado"
        return "done", (f"freno diario {txt(data['daily_loss_limit_pct'])} · "
                        f"riesgo abierto {txt(data['max_open_risk_pct'])}")
    if action == "close_all":
        path, body = "/api/close-all", {"origin": "web"}
    elif action == "close" and target:
        path, body = "/api/close", {"id": target, "origin": "web"}
    else:
        return "failed", "orden no válida"
    try:
        resp = requests.post(f"{proxy_base()}{path}", json=body, headers={"X-Webhook-Token": TOKEN}, timeout=60)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return "failed", f"proxy sin respuesta: {exc}"
    done = ", ".join(f"{d['symbol']} {'cerrada' if d['action'] == 'closed' else 'cancelada'}" for d in data.get("done") or [])
    failed = ", ".join(f"{f['symbol']}: {f['error']}" for f in data.get("failed") or [])
    text = "; ".join(x for x in (done, failed and f"falló {failed}", data.get("error")) if x) or "sin cambios"
    return ("done" if data.get("ok") else "failed"), text[:500]


def pending_commands() -> list[dict]:
    resp = requests.get(f"{URL}/rest/v1/bot_commands", headers=headers(), timeout=20, params={
        "select": "id,action,target,params,created_at", "user_id": f"eq.{OWNER}", "bot": f"eq.{BOT}",
        "status": "eq.pending", "order": "created_at.asc", "limit": "20"})
    if resp.status_code == 404 or "PGRST205" in resp.text or "does not exist" in resp.text:
        return []                             # migración 0008 aún sin aplicar
    if resp.status_code >= 300:
        raise RuntimeError(f"bot_commands: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def finish_command(cmd_id: str, status: str, result: str) -> None:
    resp = requests.patch(f"{URL}/rest/v1/bot_commands", headers=headers(), timeout=20,
                          params={"id": f"eq.{cmd_id}", "user_id": f"eq.{OWNER}"},
                          data=json.dumps({"status": status, "result": result, "done_at": iso(time.time())}))
    if resp.status_code >= 300:
        raise RuntimeError(f"bot_commands: HTTP {resp.status_code} {resp.text[:200]}")


def commands() -> int:
    """Ejecuta las órdenes pendientes de la web. Devuelve cuántas ha tratado."""
    n = 0
    for cmd in pending_commands():
        age = time.time() - datetime.fromisoformat(cmd["created_at"].replace("Z", "+00:00")).timestamp()
        if age > COMMAND_MAX_AGE:
            status, result = "failed", f"caducada: llevaba {age / 60:.0f} min sin ejecutarse"
        else:
            status, result = run_command(cmd)
        finish_command(cmd["id"], status, result)
        log(f"orden web {cmd['action']} {cmd.get('target') or ''}: {status} · {result}")
        n += 1
    return n


def main(argv: list[str]) -> int:
    missing = [n for n, v in (("SUPABASE_URL", URL), ("SUPABASE_SECRET_KEY", KEY), ("BOT_OWNER_ID", OWNER)) if not v]
    if missing:
        raise SystemExit(f"Faltan variables en .env: {', '.join(missing)}")
    sync = Sync()
    if len(argv) > 1 and argv[1] == "once":
        log(sync.once())
        return 0
    log(f"sincronizando «{BOT}» con {URL} cada {INTERVAL:g}s (órdenes de la web cada {COMMAND_POLL:g}s)")
    last_note, next_sync = "", 0.0
    while True:
        try:
            if commands():                    # tras cerrar algo, la web lo ve en la siguiente vuelta
                next_sync = 0.0
        except Exception as exc:
            log(f"error con las órdenes de la web: {exc}")
        if time.time() >= next_sync:
            try:
                note = sync.once()
                if note != last_note:        # sin cambios no llena el log
                    log(note)
                last_note = note
            except Exception as exc:          # sin red o Supabase caído: reintenta en la próxima
                log(f"error: {exc}")
            next_sync = time.time() + INTERVAL
        time.sleep(COMMAND_POLL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
