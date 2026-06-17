"""Pipeline: candidato del export -> pHash -> dedup -> visión IA -> resolver por deporte -> Mongo."""
from __future__ import annotations

import logging

from .analytics import profit_units
from .config import DEDUP_WINDOW_HOURS, PHASH_HAMMING_MAX
from .dedup import compute_phash, find_duplicate
from .ingest_export import MessageCandidate
from .models import DartsBetPayload, FootballBetPayload, PickDocument, PickResolution, TennisBetPayload
from .resolver_darts import resolve_darts_pick
from .resolver_football import resolve_football_pick
from .resolver_tennis import TennisExplorerClient, resolve_pick
from .storage import PickStore
from .vision import VisionExtractor

log = logging.getLogger(__name__)


async def _resolve(payload, event_date, te: TennisExplorerClient) -> PickResolution:
    """Enruta la resolución al resolver correcto según el deporte."""
    if isinstance(payload, TennisBetPayload):
        return await resolve_pick(payload.legs, event_date, te)
    if isinstance(payload, FootballBetPayload):
        return await resolve_football_pick(payload, event_date)
    if isinstance(payload, DartsBetPayload):
        return await resolve_darts_pick(payload, event_date)
    return PickResolution(status="no_verificable", motivo=f"deporte desconocido: {type(payload).__name__}")


async def process_candidate(
    candidate: MessageCandidate,
    store: PickStore,
    vision: VisionExtractor,
    te: TennisExplorerClient,
) -> PickDocument | None:
    tipster = candidate.channel

    if await store.exists(tipster, candidate.message_id):
        log.debug("Ya persistido tipster=%s msg=%s", tipster, candidate.message_id)
        return None

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
        payload = await vision.extract(image_bytes)
    except Exception:
        log.exception("Fallo extracción IA tipster=%s msg=%s", tipster, candidate.message_id)
        return None

    if not payload.es_pick:
        log.info("Descartado: no es pick (tipster=%s msg=%s)", tipster, candidate.message_id)
        return None

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
        resolution = await _resolve(payload, candidate.date_utc_naive.date(), te)
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
