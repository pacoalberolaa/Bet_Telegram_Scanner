"""Fuente: ESPN unofficial scoreboard API (sin key, gratis).

Endpoint:
    https://site.api.espn.com/apis/site/v2/sports/basketball/{league}/scoreboard
        ?dates=YYYYMMDD&limit=200

Ligas cubiertas: nba, wnba, mens-college-basketball, womens-college-basketball.
NO cubre Euroliga / ACB / BSL / LNB Pro A / LKL.

Cacheamos por fecha en MongoDB (colección espn_basket_games). Cada documento
agrega los partidos de TODAS las ligas ESPN para esa fecha.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import httpx
from rapidfuzz import fuzz

from .api_basketball import ApiBasketGame
from .config import API_BASKETBALL_DAY_WINDOW, API_BASKETBALL_FUZZY_THRESHOLD

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_LEAGUES = ("nba", "wnba", "mens-college-basketball", "womens-college-basketball")
ESPN_RATE_SECONDS = 0.6  # ~100 req/min, conservador


def _map_status(espn_name: str) -> str:
    """Normaliza el status de ESPN al esquema interno (FT/AOT/NS/Q1.../HT/PST/CANC)."""
    n = (espn_name or "").upper()
    if "FINAL_OVERTIME" in n: return "AOT"
    if "FINAL" in n: return "FT"
    if "HALFTIME" in n: return "HT"
    if "POSTPONED" in n: return "PST"
    if "CANCEL" in n: return "CANC"
    if "SUSPEND" in n: return "ABD"
    if "FORFEIT" in n: return "AWD"
    if "END_PERIOD" in n or "IN_PROGRESS" in n: return "Q4"  # genérico "vivo"
    if "PRE" in n or "SCHEDULED" in n: return "NS"
    return n or "UNK"


def _to_int(v: Any) -> int | None:
    if v is None: return None
    try: return int(float(v))
    except (TypeError, ValueError): return None


def _parse_event(event: dict, d: date, league: str) -> ApiBasketGame | None:
    try:
        comps = event.get("competitions") or []
        if not comps:
            return None
        comp = comps[0]
        status_name = ((comp.get("status") or {}).get("type") or {}).get("name", "")
        status = _map_status(status_name)
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            return None

        def _team_name(c):
            t = c.get("team") or {}
            return t.get("displayName") or t.get("name") or t.get("shortDisplayName") or ""

        def _line_q(c) -> tuple[list[int | None], int | None]:
            ls = c.get("linescores") or []
            vals = [_to_int(x.get("value")) for x in ls]
            quarters: list[int | None] = [None, None, None, None]
            for i in range(min(4, len(vals))):
                quarters[i] = vals[i]
            ot = sum(v for v in vals[4:] if v is not None) if len(vals) > 4 else None
            return quarters, ot

        home_q, home_ot = _line_q(home)
        away_q, away_ot = _line_q(away)

        return ApiBasketGame(
            game_id=int(event.get("id", 0)) if str(event.get("id", "")).isdigit() else hash(event.get("id", "")) & 0x7FFFFFFF,
            league=f"ESPN/{league}",
            status=status,
            team_home=_team_name(home),
            team_away=_team_name(away),
            home_q=home_q, away_q=away_q,
            home_ot=home_ot, away_ot=away_ot,
            home_total=_to_int(home.get("score")),
            away_total=_to_int(away.get("score")),
            match_date=d,
        )
    except Exception:
        log.exception("Fallo parseando evento ESPN: %r", event)
        return None


def _game_from_dict(d: dict[str, Any]) -> ApiBasketGame:
    md = d.get("match_date")
    if isinstance(md, str):
        try:
            md = date.fromisoformat(md)
        except ValueError:
            md = None
    return ApiBasketGame(
        game_id=int(d.get("game_id", 0)),
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


class EspnBasketClient:
    def __init__(self, store, rate_seconds: float = ESPN_RATE_SECONDS) -> None:
        self._store = store
        self._rate = rate_seconds
        self._client = httpx.AsyncClient(
            base_url=ESPN_BASE,
            headers={"User-Agent": "BetTelegramScannerBot/0.1 research"},
            timeout=20.0,
        )
        self._memory: dict[date, list[ApiBasketGame]] = {}
        self._fetch_lock = asyncio.Lock()
        self._date_locks: dict[date, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()

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
            cached = await self._store.get_espn_basket_games(d)
            if cached is not None:
                games = [_game_from_dict(g) for g in cached]
                self._memory[d] = games
                return games

            games: list[ApiBasketGame] = []
            yyyymmdd = d.strftime("%Y%m%d")
            fetch_ok_any = False
            for league in ESPN_LEAGUES:
                try:
                    async with self._fetch_lock:
                        await asyncio.sleep(self._rate)
                        r = await self._client.get(
                            f"/{league}/scoreboard",
                            params={"dates": yyyymmdd, "limit": 200},
                        )
                    if r.status_code != 200:
                        log.warning("ESPN %s %s -> HTTP %s", league, d, r.status_code)
                        continue
                    data = r.json()
                    for ev in data.get("events", []) or []:
                        g = _parse_event(ev, d, league)
                        if g is not None:
                            games.append(g)
                    fetch_ok_any = True
                except Exception:
                    log.exception("Fallo ESPN %s %s", league, d)

            if fetch_ok_any:
                await self._store.save_espn_basket_games(d, [asdict(g) for g in games])
                self._memory[d] = games
            return games

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
