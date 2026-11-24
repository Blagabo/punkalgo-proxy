"""
classify.py — qué tipo de mensaje publica el canal de señales.

    signal      señal operable: par, Long/Short, Entrada, Stop Loss y objetivos
    target_hit  "Objetivo N Tocado" (beneficio en % apalancado, periodo, aviso de BE)
    sl_hit      "Stop Loss Alcanzado" (pérdida en % apalancado)
    pump        "PAR/USDT Pumpeando +X%" (informativo, no se opera)
    other       cualquier otra cosa

Sólo `signal` se manda al proxy; el resto se guarda para el journal y para comparar lo que
dice el canal con lo que mide el bot.
"""

from __future__ import annotations

import re
from typing import Any

RE_PAIR = re.compile(r"#?\s*([A-Z0-9]{2,20})\s*/\s*(USDT|USDC|USD)\b", re.I)
RE_SIDE = re.compile(r"\b(long|short)\b", re.I)
RE_ENTRY = re.compile(r"\bentrada\b", re.I)
RE_SL = re.compile(r"\bstop\s*loss\b", re.I)
RE_TARGET = re.compile(r"\bobjetivo\s*\d", re.I)
RE_HIT = re.compile(r"\bobjetivo\s*(\d+)\s*tocado", re.I)
RE_SL_HIT = re.compile(r"stop\s*loss\s*alcanzado", re.I)
RE_LOSS = re.compile(r"p[ée]rdida\s*:?\s*([+-]?\d+(?:[.,]\d+)?)\s*%", re.I)
RE_PROFIT = re.compile(r"beneficio\s*:?\s*([+-]?\d+(?:[.,]\d+)?)\s*%", re.I)
RE_PERIOD = re.compile(r"periodo\s*:?\s*(?:(\d+)\s*horas?)?\s*(?:(\d+)\s*minutos?)?", re.I)
RE_BE = re.compile(r"break\s*even|sl\s+a\s+la\s+entrada", re.I)
RE_PUMP = re.compile(r"pumpeando\s*([+-]?\d+(?:[.,]\d+)?)\s*%", re.I)
RE_PRICE = re.compile(r"precio\s*:?\s*([0-9]+(?:[.,][0-9]+)?)", re.I)


def _num(raw: str | None) -> float | None:
    return float(raw.replace(",", ".")) if raw else None


def _symbol(text: str) -> str | None:
    m = RE_PAIR.search(text)
    return (m.group(1) + m.group(2)).upper() if m else None


def classify(text: str) -> dict[str, Any]:
    """Devuelve {"type": ..., "symbol": ..., ...campos del tipo}."""
    text = text or ""
    symbol = _symbol(text)

    hit = RE_HIT.search(text)
    if hit:
        period = RE_PERIOD.search(text)
        minutes = None
        if period and (period.group(1) or period.group(2)):
            minutes = int(period.group(1) or 0) * 60 + int(period.group(2) or 0)
        return {"type": "target_hit", "symbol": symbol, "target": int(hit.group(1)),
                "profit_pct": _num(m.group(1)) if (m := RE_PROFIT.search(text)) else None,
                "minutes": minutes, "move_to_be": bool(RE_BE.search(text))}

    if RE_SL_HIT.search(text):
        return {"type": "sl_hit", "symbol": symbol,
                "loss_pct": _num(m.group(1)) if (m := RE_LOSS.search(text)) else None}

    pump = RE_PUMP.search(text)
    if pump:
        return {"type": "pump", "symbol": symbol, "change_pct": _num(pump.group(1)),
                "price": _num(m.group(1)) if (m := RE_PRICE.search(text)) else None}

    if (symbol and RE_SIDE.search(text) and RE_ENTRY.search(text)
            and RE_SL.search(text) and RE_TARGET.search(text)):
        return {"type": "signal", "symbol": symbol, "side": RE_SIDE.search(text).group(1).upper()}

    return {"type": "other", "symbol": symbol}
