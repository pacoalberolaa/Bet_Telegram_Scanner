"""Pipeline: candidato del export -> pHash -> dedup -> visión IA -> dedup semántico -> resolver -> Mongo."""
from __future__ import annotations

import logging
import re

from pydantic import TypeAdapter

from .analytics import profit_units
from .config import DEDUP_WINDOW_HOURS, PHASH_HAMMING_MAX
from .dedup import compute_phash, find_duplicate
from .ingest_export import MessageCandidate
from .models import BasketballBetPayload, BasketballLeg, DartsBetPayload, DartsLeg, FootballBetPayload, FootballLeg, PickDocument, PickResolution, TennisBetPayload, TennisLeg
from .quality import LowConfidenceLogger, detect_low_confidence
from .resolver_basketball import BasketballResultsClient, resolve_basketball_pick
from .resolver_darts import resolve_darts_pick
from .resolver_football import resolve_football_pick
from .resolver_tennis import TennisExplorerClient, resolve_pick
from .storage import PickStore
from .vision import VisionExtractor

log = logging.getLogger(__name__)

_PICK_ADAPTER = TypeAdapter(PickDocument)

_MEDIA_TYPE_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _guess_media_type(path) -> str:
    return _MEDIA_TYPE_BY_EXT.get(path.suffix.lower(), "image/jpeg")


SEMANTIC_DEDUP_WINDOW_HOURS = 72


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _leg_signature(leg) -> tuple:
    """Identidad semántica de una pierna: lo que la hace única dentro de un pick."""
    if isinstance(leg, (FootballLeg, BasketballLeg)):
        teams = tuple(sorted([_norm(leg.equipo_local), _norm(leg.equipo_visitante)]))
    elif isinstance(leg, (TennisLeg, DartsLeg)):
        teams = tuple(sorted([_norm(leg.jugador_1), _norm(leg.jugador_2)]))
    else:
        teams = ()
    periodo = getattr(leg, "periodo", None)
    over_under = getattr(leg, "over_under", None)
    return (
        teams,
        leg.mercado,
        _norm(leg.seleccion),
        leg.linea,
        over_under,
        periodo,
    )


def _payload_signature(payload) -> frozenset:
    return frozenset(_leg_signature(l) for l in payload.legs)


def _find_semantic_duplicate(payload, recent_docs: list[dict]) -> dict | None:
    """Devuelve el documento existente que es semánticamente igual, o None.
    Requiere coincidencia EXACTA en todas las legs (equipos/jugadores normalizados,
    mercado, selección, línea, over/under, periodo). Solo aplica si hay legs."""
    if not payload.legs:
        return None
    sig_new = _payload_signature(payload)
    adapter = _PICK_ADAPTER
    for doc in recent_docs:
        try:
            existing = adapter.validate_python(doc)
        except Exception:
            continue
        if not existing.payload.legs:
            continue
        if _payload_signature(existing.payload) == sig_new:
            return doc
    return None


async def _resolve(payload, event_date, te: TennisExplorerClient, api_basket: BasketballResultsClient) -> PickResolution:
    """Enruta la resolución al resolver correcto según el deporte."""
    if isinstance(payload, TennisBetPayload):
        return await resolve_pick(payload.legs, event_date, te)
    if isinstance(payload, FootballBetPayload):
        return await resolve_football_pick(payload, event_date)
    if isinstance(payload, DartsBetPayload):
        return await resolve_darts_pick(payload, event_date)
    if isinstance(payload, BasketballBetPayload):
        return await resolve_basketball_pick(payload, event_date, api_basket)
    return PickResolution(status="no_verificable", motivo=f"deporte desconocido: {type(payload).__name__}")


async def _refresh_existing(
    candidate: MessageCandidate,
    existing_doc: dict,
    store: PickStore,
    vision: VisionExtractor,
    te: TennisExplorerClient,
    api_basket: BasketballResultsClient,
) -> PickDocument | None:
    """Re-procesa un pick que ya está en Mongo:
      - si su payload tenía es_pick=true pero legs=[], re-extrae con visión
        (el fallback a Opus puede leer ahora lo que haiku no pudo);
      - re-resuelve si la resolución previa era None/no_verificable, o si la
        visión cambió (legs antes vacías → ahora rellenas).
    Devuelve el PickDocument actualizado, o None si no había nada que cambiar."""
    tipster = candidate.channel
    existing = _PICK_ADAPTER.validate_python(existing_doc)
    payload = existing.payload
    prev_status = existing.resolution.status if existing.resolution else None

    needs_revision = bool(payload.es_pick and not payload.legs)
    needs_resolve = prev_status in (None, "no_verificable")

    if not needs_revision and not needs_resolve:
        return None

    if needs_revision:
        try:
            image_bytes = candidate.photo_path.read_bytes()
        except OSError:
            log.exception("Fallo leyendo foto para re-vision tipster=%s msg=%s",
                          tipster, candidate.message_id)
            image_bytes = b""
        if image_bytes:
            try:
                new_payload = await vision.extract(
                    image_bytes, media_type=_guess_media_type(candidate.photo_path),
                )
            except Exception:
                log.exception("Fallo re-extracción IA tipster=%s msg=%s",
                              tipster, candidate.message_id)
                new_payload = payload
            if new_payload.es_pick and new_payload.legs:
                log.info(
                    "Re-vision rescató legs=%d tipster=%s msg=%s",
                    len(new_payload.legs), tipster, candidate.message_id,
                )
                await store.update_payload(tipster, candidate.message_id,
                                           new_payload.model_dump(mode="python"))
                payload = new_payload
                needs_resolve = True

    if not needs_resolve:
        return None

    try:
        resolution = await _resolve(payload, existing.date_utc.date(), te, api_basket)
    except Exception:
        log.exception("Fallo re-resolviendo tipster=%s msg=%s", tipster, candidate.message_id)
        resolution = PickResolution(status="no_verificable", motivo="error en resolver")

    profit = profit_units(payload, resolution)
    await store.update_resolution(tipster, candidate.message_id, resolution, profit)
    updated = existing.model_copy(update={
        "payload": payload, "resolution": resolution, "profit_units": profit,
    })
    log.info(
        "Re-resolve tipster=%s msg=%s legs=%d resol=%s (antes %s) profit=%s",
        tipster, candidate.message_id, len(payload.legs),
        resolution.status, prev_status, profit,
    )
    return updated


async def process_candidate(
    candidate: MessageCandidate,
    store: PickStore,
    vision: VisionExtractor,
    te: TennisExplorerClient,
    api_basket: BasketballResultsClient,
    low_conf: LowConfidenceLogger | None = None,
) -> PickDocument | None:
    tipster = candidate.channel

    existing_doc = await store.get_pick(tipster, candidate.message_id)
    if existing_doc is not None:
        return await _refresh_existing(
            candidate, existing_doc, store, vision, te, api_basket,
        )

    try:
        image_bytes = candidate.photo_path.read_bytes()
    except OSError:
        log.exception("Fallo leyendo foto tipster=%s msg=%s path=%s",
                      tipster, candidate.message_id, candidate.photo_path)
        return None
    if not image_bytes:
        log.warning("Foto vacía tipster=%s msg=%s", tipster, candidate.message_id)
        return None

    try:
        phash = compute_phash(image_bytes)
    except Exception:
        log.exception("Fallo computando pHash tipster=%s msg=%s", tipster, candidate.message_id)
        return None

    dup = await find_duplicate(
        store, tipster, phash, candidate.date_utc_naive,
        window_hours=DEDUP_WINDOW_HOURS, max_distance=PHASH_HAMMING_MAX,
    )
    if dup is not None:
        log.info(
            "Dedup hit tipster=%s msg=%s ~ prev_msg=%s dist=%d",
            tipster, candidate.message_id, dup.message_id, dup.distance,
        )
        return None

    try:
        payload = await vision.extract(image_bytes, media_type=_guess_media_type(candidate.photo_path))
    except Exception:
        log.exception("Fallo extracción IA tipster=%s msg=%s", tipster, candidate.message_id)
        return None

    if not payload.es_pick:
        log.info("Descartado: no es pick (tipster=%s msg=%s)", tipster, candidate.message_id)
        return None

    recent = await store.recent_picks_full(
        tipster, candidate.date_utc_naive, window_hours=SEMANTIC_DEDUP_WINDOW_HOURS,
    )
    dup_doc = _find_semantic_duplicate(payload, recent)
    if dup_doc is not None:
        dup_msg = dup_doc.get("message_id")
        dup_date = dup_doc.get("date_utc")
        if dup_date is not None and candidate.date_utc_naive < dup_date:
            log.info(
                "Dedup semántico: nuevo msg=%s (%s) es más antiguo que existente msg=%s (%s); "
                "sustituyo identidad para preservar fecha del pendiente",
                candidate.message_id, candidate.date_utc_naive, dup_msg, dup_date,
            )
            await store.replace_pick_identity(
                tipster, dup_msg, candidate.message_id, candidate.date_utc_naive,
            )
        else:
            log.info(
                "Dedup semántico hit tipster=%s msg=%s ~ prev_msg=%s (mismo pick)",
                tipster, candidate.message_id, dup_msg,
            )
        return None

    if low_conf is not None:
        reasons = detect_low_confidence(payload)
        if reasons:
            await low_conf.log(candidate, payload, reasons)

    pick = PickDocument(
        tipster=tipster,
        message_id=candidate.message_id,
        date_utc=candidate.date_utc_naive,
        phash=phash,
        text_raw=candidate.raw_text,
        payload=payload,
    )
    await store.insert_pick(pick)

    try:
        resolution = await _resolve(payload, candidate.date_utc_naive.date(), te, api_basket)
    except Exception:
        log.exception("Fallo resolviendo tipster=%s msg=%s", tipster, candidate.message_id)
        resolution = PickResolution(status="no_verificable", motivo="error en resolver")

    profit = profit_units(payload, resolution)
    await store.update_resolution(tipster, candidate.message_id, resolution, profit)

    pick = pick.model_copy(update={"resolution": resolution, "profit_units": profit})
    log.info(
        "Pick tipster=%s msg=%s legs=%d resol=%s profit=%s",
        tipster, candidate.message_id, len(payload.legs), resolution.status, profit,
    )
    return pick
