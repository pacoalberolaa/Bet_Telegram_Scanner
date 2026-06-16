"""Extractor histórico asíncrono sobre Telethon.iter_messages.

Recorre el canal hacia atrás desde `request.end` hasta `request.start`,
descarta ruido a nivel de metadatos (reenvíos, respuestas, mensajes sin
foto) y emite candidatos listos para el pipeline de pHash + visión IA.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.custom.message import Message

from .cli import ScanRequest
from .config import BATCH_LOG_EVERY, TELEGRAM_PAUSE_SECONDS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageCandidate:
    """Mensaje pre-filtrado que sí merece análisis de imagen."""
    channel: str
    message_id: int
    date_utc_naive: datetime
    raw_text: str
    message: Message  # se mantiene para descargar el media downstream


def _to_naive_utc(dt: datetime) -> datetime:
    """Telegram devuelve datetimes aware en UTC; los normalizamos a naive."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _is_noise(msg: Message) -> tuple[bool, str]:
    if msg.fwd_from is not None:
        return True, "reenviado"
    if msg.reply_to_msg_id is not None:
        return True, "respuesta"
    if msg.photo is None:
        return True, "sin_foto"
    return False, ""


async def iter_historical_candidates(
    client: TelegramClient,
    request: ScanRequest,
) -> AsyncIterator[MessageCandidate]:
    """Itera el historial del canal en la ventana [start, end].

    Telethon `iter_messages` con `offset_date` recorre hacia atrás en el
    tiempo, así que arrancamos desde `end` y cortamos al cruzar `start`.
    Cada `FloodWaitError` se respeta exactamente el tiempo solicitado.
    """
    log.info(
        "Inicio escaneo canal=%s ventana=[%s .. %s]",
        request.channel, request.start.isoformat(), request.end.isoformat(),
    )

    seen = 0
    emitted = 0
    discarded: dict[str, int] = {}

    # offset_date debe ser aware para Telethon.
    offset = request.end.replace(tzinfo=timezone.utc)

    iterator = client.iter_messages(request.channel, offset_date=offset)

    while True:
        try:
            msg: Message | None = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except FloodWaitError as e:
            wait = int(e.seconds) + 1
            log.warning("FloodWait de Telegram: durmiendo %ss", wait)
            await asyncio.sleep(wait)
            continue

        if msg is None:
            break

        msg_date = _to_naive_utc(msg.date)
        if msg_date < request.start:
            log.info("Cruzamos fecha_inicio (%s); fin de escaneo.", msg_date)
            break

        seen += 1

        is_noise, reason = _is_noise(msg)
        if is_noise:
            discarded[reason] = discarded.get(reason, 0) + 1
        else:
            emitted += 1
            yield MessageCandidate(
                channel=request.channel,
                message_id=msg.id,
                date_utc_naive=msg_date,
                raw_text=msg.message or "",
                message=msg,
            )

        if seen % BATCH_LOG_EVERY == 0:
            log.info(
                "Progreso: vistos=%d emitidos=%d descartados=%s (cursor=%s)",
                seen, emitted, discarded, msg_date.isoformat(),
            )

        await asyncio.sleep(TELEGRAM_PAUSE_SECONDS)

    log.info(
        "Escaneo finalizado. vistos=%d emitidos=%d descartados=%s",
        seen, emitted, discarded,
    )
