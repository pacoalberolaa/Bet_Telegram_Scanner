"""Cliente HTTP para API-Sports Basketball (v1.basketball.api-sports.io).

Free tier: 100 req/día. Cacheamos por fecha en MongoDB para amortizar.
Patrón calcado de `resolver_tennis.TennisExplorerClient`:
    - _fetch_lock global para rate-limit aunque haya N workers paralelos
    - _date_locks por fecha para evitar fetch duplicado del mismo día
    - _memory in-process + cache Mongo via store.get/save_api_basketball_games
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from rapidfuzz import fuzz

from .config import (
    API_BASKETBALL_BASE_URL,
    API_BASKETBALL_DAY_WINDOW,
    API_BASKETBALL_FUZZY_THRESHOLD,
    API_BASKETBALL_RATE_SECONDS,
)

log = logging.getLogger(__name__)


@dataclass
class ApiBasketGame:
    game_id: int
    league: str
    status: str  # FT, AOT (after OT), NS, Q1..Q4, HT, OT, BT, PST, CANC, ABD, AWD
    team_home: str
    team_away: str
    home_q: list[int | None] = field(default_factory=lambda: [None, None, None, None])
    away_q: list[int | None] = field(default_factory=lambda: [None, None, None, None])
    home_ot: int | None = None
    away_ot: int | None = None
    home_total: int | None = None
    away_total: int | None = None
    match_date: date | None = None


def _parse_game(p: dict[str, Any]) -> ApiBasketGame | None:
    try:
        teams = p.get("teams") or {}
        scores = p.get("scores") or {}
        home_s = scores.get("home") or {}
        away_s = scores.get("away") or {}
        status = (p.get("status") or {}).get("short", "") or ""
        league = (p.get("league") or {}).get("name", "") or ""
        raw_date = p.get("date")
        md: date | None = None
        if isinstance(raw_date, str):
            try:
                md = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
            except ValueError:
                md = None
        return ApiBasketGame(
            game_id=int(p["id"]),
            league=str(league),
            status=str(status),
            team_home=str((teams.get("home") or {}).get("name", "") or ""),
            team_away=str((teams.get("away") or {}).get("name", "") or ""),
            home_q=[
                home_s.get("quarter_1"), home_s.get("quarter_2"),
                home_s.get("quarter_3"), home_s.get("quarter_4"),
            ],
            away_q=[
                away_s.get("quarter_1"), away_s.get("quarter_2"),
                away_s.get("quarter_3"), away_s.get("quarter_4"),
            ],
            home_ot=home_s.get("over_time"),
            away_ot=away_s.get("over_time"),
            home_total=home_s.get("total"),
            away_total=away_s.get("total"),
            match_date=md,
        )
    except Exception:
        log.exception("Fallo parseando game API-Sports: %r", p)
        return None


_QUOTA_KEYWORDS = ("limit", "quota", "rate")
_PLAN_KEYWORDS = ("plan", "subscription", "access to this date")


def _errors_indicate_quota(errors: Any) -> bool:
    """Detecta agotamiento de cuota diaria (cuenta sin más requests).

    Distinguimos de restricciones por plan (p.ej. fechas históricas no
    accesibles en Free): esas no son cuota, son features bloqueadas.
    """
    if not errors:
        return False
    items: list[tuple[str, str]] = []
    if isinstance(errors, dict):
        items = [(str(k).lower(), str(v).lower()) for k, v in errors.items()]
    elif isinstance(errors, list):
        items = [("", str(x).lower()) for x in errors]
    for key, value in items:
        blob = f"{key} {value}"
        # Si claramente menciona restricción de plan/fecha, NO es quota.
        if any(p in blob for p in _PLAN_KEYWORDS) and "limit" not in blob:
            continue
        # quota real: "requests" + "limit", "rate limit", "daily limit", etc.
        if key in ("requests", "ratelimit", "dailylimit"):
            return True
        if any(k in value for k in _QUOTA_KEYWORDS):
            return True
    return False


def _errors_indicate_plan_restriction(errors: Any) -> bool:
    """Detecta 'esta fecha/endpoint no está en tu plan' (restricción por suscripción)."""
    if not errors:
        return False
    items: list[tuple[str, str]] = []
    if isinstance(errors, dict):
        items = [(str(k).lower(), str(v).lower()) for k, v in errors.items()]
    elif isinstance(errors, list):
        items = [("", str(x).lower()) for x in errors]
    for key, value in items:
        blob = f"{key} {value}"
        if any(p in blob for p in _PLAN_KEYWORDS):
            return True
    return False


def _game_from_dict(d: dict[str, Any]) -> ApiBasketGame:
    md = d.get("match_date")
    if isinstance(md, str):
        try:
            md = date.fromisoformat(md)
        except ValueError:
            md = None
    elif isinstance(md, datetime):
        md = md.date()
    return ApiBasketGame(
        game_id=int(d["game_id"]),
        league=d.get("league", ""),
        status=d.get("status", ""),
        team_home=d.get("team_home", ""),
        team_away=d.get("team_away", ""),
        home_q=list(d.get("home_q", [None] * 4)),
        away_q=list(d.get("away_q", [None] * 4)),
        home_ot=d.get("home_ot"),
        away_ot=d.get("away_ot"),
        home_total=d.get("home_total"),
        away_total=d.get("away_total"),
        match_date=md,
    )


class ApiBasketballClient:
    def __init__(self, store, rate_seconds: float = API_BASKETBALL_RATE_SECONDS) -> None:
        self._store = store
        self._rate = rate_seconds
        self._api_key = os.environ.get("API_BASKETBALL_KEY", "")
        headers = {"x-apisports-key": self._api_key} if self._api_key else {}
        self._client = httpx.AsyncClient(
            base_url=API_BASKETBALL_BASE_URL,
            headers=headers,
            timeout=20.0,
        )
        self._memory: dict[date, list[ApiBasketGame]] = {}
        self._fetch_lock = asyncio.Lock()
        self._date_locks: dict[date, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()
        # Estado del backend, consultable por el resolver para afinar el motivo.
        self._quota_exceeded = False
        self._last_error: str | None = None

    @property
    def quota_exceeded(self) -> bool:
        return self._quota_exceeded

    def unavailable_reason(self) -> str | None:
        """None si el cliente está operativo; motivo si no se pudo consultar."""
        if not self._api_key:
            return "API_BASKETBALL_KEY no configurada"
        if self._quota_exceeded:
            return "cuota diaria de API-Sports agotada"
        return self._last_error

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _date_lock(self, d: date) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._date_locks.get(d)
            if lock is None:
                lock = asyncio.Lock()
                self._date_locks[d] = lock
            return lock

    async def games_on(self, d: date) -> list[ApiBasketGame]:
        if d in self._memory:
            return self._memory[d]

        date_lock = await self._date_lock(d)
        async with date_lock:
            if d in self._memory:
                return self._memory[d]
            cached = await self._store.get_api_basketball_games(d)
            if cached is not None:
                games = [_game_from_dict(g) for g in cached]
                self._memory[d] = games
                return games

            if not self._api_key:
                log.warning(
                    "API_BASKETBALL_KEY no configurada; baloncesto no se podrá verificar.",
                )
                self._memory[d] = []
                return []

            if self._quota_exceeded:
                # Una vez agotada la cuota, no insistimos con más requests hasta reinicio.
                self._memory[d] = []
                return []

            games: list[ApiBasketGame] = []
            fetch_ok = False
            try:
                async with self._fetch_lock:
                    await asyncio.sleep(self._rate)
                    r = await self._client.get("/games", params={"date": d.isoformat()})
                if r.status_code == 429 or self._is_quota_response(r):
                    self._quota_exceeded = True
                    self._last_error = "cuota diaria de API-Sports agotada"
                    log.error("API-Sports basket: cuota agotada (HTTP %s). "
                              "Resto de picks de basket saldrán no_verificable.", r.status_code)
                elif r.status_code != 200:
                    self._last_error = f"HTTP {r.status_code}"
                    log.warning("API-Sports basket %s -> HTTP %s body=%s",
                                d, r.status_code, r.text[:200])
                else:
                    data = r.json()
                    errors = data.get("errors")
                    if _errors_indicate_quota(errors):
                        self._quota_exceeded = True
                        self._last_error = "cuota diaria de API-Sports agotada"
                        log.error("API-Sports basket: cuota agotada (errors=%s). "
                                  "Resto de picks de basket saldrán no_verificable.", errors)
                    elif _errors_indicate_plan_restriction(errors):
                        # Fecha no incluida en el plan (típico en Free: solo ±1 día).
                        # No es un fallo global; otras fechas (recientes) podrían funcionar.
                        # La cacheamos como vacía para no reintentarla en cada pick.
                        self._last_error = (
                            f"fecha {d} no disponible en tu plan de API-Sports "
                            f"(detalle: {errors})"
                        )
                        log.warning("API-Sports basket %s -> %s", d, self._last_error)
                        self._memory[d] = []
                    elif errors:
                        self._last_error = f"errors={errors}"
                        log.warning("API-Sports basket %s errors=%s", d, errors)
                    else:
                        for raw in data.get("response", []) or []:
                            g = _parse_game(raw)
                            if g is not None:
                                games.append(g)
                        fetch_ok = True
                        self._last_error = None
            except Exception as exc:
                self._last_error = f"excepción: {exc!r}"
                log.exception("Fallo consultando API-Sports basket %s", d)

            if fetch_ok:
                # Solo cacheamos cuando la consulta fue limpia: un fallo no debe
                # quedar grabado como "ese día no hubo partidos".
                await self._store.save_api_basketball_games(d, [asdict(g) for g in games])
                self._memory[d] = games
            # En caso de fallo no escribimos en _memory para que se reintente
            # en próximos picks (salvo quota_exceeded, donde el guard de arriba evita más HTTP).
            if not fetch_ok and self._quota_exceeded:
                self._memory[d] = []
            return games

    @staticmethod
    def _is_quota_response(r: httpx.Response) -> bool:
        # Algunos planes devuelven 499 / 403 + cuerpo indicando cuota agotada.
        try:
            data = r.json()
        except Exception:
            return False
        return _errors_indicate_quota(data.get("errors"))

    async def find_game(
        self,
        team_a: str,
        team_b: str,
        around: date,
        day_window: int = API_BASKETBALL_DAY_WINDOW,
    ) -> ApiBasketGame | None:
        best: tuple[int, ApiBasketGame] | None = None
        for offset in range(-day_window, day_window + 1):
            d = around + timedelta(days=offset)
            for g in await self.games_on(d):
                score = _pair_score(team_a, team_b, g.team_home, g.team_away)
                if score >= API_BASKETBALL_FUZZY_THRESHOLD and (best is None or score > best[0]):
                    best = (score, g)
        return best[1] if best else None


# ---------------------------------------------------------------------------
# Fuzzy match equipos
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _team_score(a: str, b: str) -> int:
    return int(fuzz.token_set_ratio(_normalize(a), _normalize(b)))


def _pair_score(bet_a: str, bet_b: str, home: str, away: str) -> int:
    s_direct = (_team_score(bet_a, home) + _team_score(bet_b, away)) // 2
    s_swap = (_team_score(bet_a, away) + _team_score(bet_b, home)) // 2
    return max(s_direct, s_swap)
