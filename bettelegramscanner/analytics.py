"""Cálculo de yield/ROI por tipster sobre picks VERIFICADOS externamente."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import BetPayload, PickDocument, PickResolution

log = logging.getLogger(__name__)


def profit_units(
    payload: BetPayload,
    resolution: PickResolution,
    stake_fallback: float = 1.0,
) -> float | None:
    """Profit del boleto según la resolución real.

    - ganada  -> stake * (cuota_total - 1)
    - perdida -> -stake
    - void    -> 0
    - no_verificable -> None (no entra al yield)
    """
    if resolution.status == "no_verificable":
        return None
    stake = payload.stake_indicado if payload.stake_indicado is not None else stake_fallback
    match resolution.status:
        case "ganada":
            return round(stake * (payload.cuota_total - 1.0), 4)
        case "perdida":
            return round(-stake, 4)
        case "void":
            return 0.0
    return None


@dataclass(frozen=True)
class TipsterReport:
    tipster: str
    total_picks: int
    verificados: int
    no_verificables: int
    ganados: int
    perdidos: int
    voids: int
    stake_total: float
    profit_total: float
    yield_pct: float

    def render(self) -> str:
        return (
            f"[{self.tipster}] picks={self.total_picks} "
            f"verif={self.verificados} no_verif={self.no_verificables} "
            f"W/L/V={self.ganados}/{self.perdidos}/{self.voids} "
            f"stake={self.stake_total:.2f}u profit={self.profit_total:+.2f}u "
            f"yield={self.yield_pct:+.2f}%"
        )


def build_report(tipster: str, picks: list[PickDocument]) -> TipsterReport:
    verificados = no_verificables = 0
    ganados = perdidos = voids = 0
    stake_total = 0.0
    profit_total = 0.0

    for p in picks:
        if p.resolution is None or p.resolution.status == "no_verificable":
            no_verificables += 1
            continue
        verificados += 1
        stake = p.payload.stake_indicado if p.payload.stake_indicado is not None else 1.0
        stake_total += stake
        if p.profit_units is not None:
            profit_total += p.profit_units
        match p.resolution.status:
            case "ganada":
                ganados += 1
            case "perdida":
                perdidos += 1
            case "void":
                voids += 1

    yield_pct = (profit_total / stake_total * 100.0) if stake_total > 0 else 0.0

    return TipsterReport(
        tipster=tipster,
        total_picks=len(picks),
        verificados=verificados,
        no_verificables=no_verificables,
        ganados=ganados,
        perdidos=perdidos,
        voids=voids,
        stake_total=round(stake_total, 4),
        profit_total=round(profit_total, 4),
        yield_pct=round(yield_pct, 4),
    )
