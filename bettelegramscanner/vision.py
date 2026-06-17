"""Extracción estructurada de boletos de apuestas multi-deporte vía Claude Opus 4.7 (visión).

Flujo de 2 fases por imagen:
  1. Detección rápida: ¿es un boleto? ¿qué deporte?
  2. Extracción especializada con prompt y schema del deporte detectado.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from .config import LLM_PAUSE_SECONDS
from .models import AnyBetPayload, BasketballBetPayload, DartsBetPayload, FootballBetPayload, TennisBetPayload

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
FALLBACK_MODEL = "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Fase 1 — Detección de deporte
# ---------------------------------------------------------------------------

class SportDetection(BaseModel):
    es_pick: bool
    sport: Literal["tennis", "football", "darts", "basketball", "other"]


DETECTION_PROMPT = """Eres un clasificador de imágenes de boletos de apuestas deportivas.

Devuelve SOLO el JSON con dos campos:
- es_pick (boolean): true si la imagen es un boleto/ticket de apuesta NUEVO con selección y cuota visible.
  false si es cualquier otra cosa: stats, rachas, memes, banners, capturas de menú, mensajes de texto sin ticket.
  false TAMBIÉN si la imagen es una REPUBLICACIÓN CELEBRATORIA de un pick ya resuelto:
    * banners grandes con texto tipo "¡APUESTA ACERTADA!", "¡ACERTADA!", "¡GANADA!", "WINNER",
      "¡SEGUIMOOOOOS!", "¡PERDIDA!", "¡FALLADA!", "¡QUÉ PENA!".
    * checks/ticks verdes GIGANTES superpuestos al ticket, cruces rojas grandes, coronas, fuegos artificiales,
      llamas, billetes/dinero flotantes, fondos verde fluor o rojo brillante.
    * marcador final del partido pintado dentro del propio ticket junto a los equipos
      (ej: "TOR Raptors 102 / CLE Cavaliers 114") cuando claramente es resultado YA jugado,
      no la previa.
  Estos recaps son duplicados de un pick original que el tipster ya publicó y NO deben procesarse de nuevo.
- sport (string): el deporte del boleto. Valores permitidos: "tennis", "football", "darts", "basketball", "other".
  Solo aplica cuando es_pick=true. Si es_pick=false, pon "other".

Indicios por deporte:
- tennis: nombres de jugadores individuales, sets, juegos, Roland Garros, ATP, WTA, Wimbledon.
- football: nombres de equipos, Liga, Premier, Champions, goles, corners, BTTS, 1X2.
- darts: dardos, PDC, Premier League Darts, legs, 180s, checkout, nombres como Van Gerwen, Wright, Price.
- basketball: NBA, ACB, Euroliga, Eurocup, NCAA, equipos como Lakers, Celtics, Real Madrid Basket, Barça Basket, jugadores como LeBron, Doncic, Curry, Llull; spreads decimales (-5.5, +7.5), totales altos (190+), conceptos como "puntos", "rebotes", "asistencias", "triples", "cuarto", "Q1/Q2/Q3/Q4".
- other: cualquier otro deporte (hockey, béisbol, rugby, MMA…).

Responde EXCLUSIVAMENTE el JSON. Sin texto extra, sin markdown."""


# ---------------------------------------------------------------------------
# Fase 2 — Prompts especializados por deporte
# ---------------------------------------------------------------------------

TENNIS_PROMPT = """Eres un analista experto en boletos de apuestas de TENIS publicados por tipsters en Telegram.

Tu función es extraer los datos estructurados del boleto para que un sistema externo pueda VERIFICAR el resultado real contra Tennis Explorer. La precisión en jugadores, mercado, selección y línea es crítica. Nada de inferir resultados; nada de marcar ganada/perdida.

# Reglas generales
- Devuelve EXCLUSIVAMENTE el JSON conforme al esquema. Sin texto extra, sin markdown.
- Si un campo no es legible y es opcional, devuelve null. Nunca inventes.
- Conserva el idioma original (no traduzcas nombres de jugadores ni de torneos).
- No incluyas emojis, banderas, ni adornos en los strings.
- sport SIEMPRE debe ser "tennis".

# es_pick
- true: la imagen es un ticket de apuesta con cuota y selección visibles.
- false: ruido (stats, racha, meme, banner, captura vacía, texto sin boleto). Si es false, el resto puede quedar vacío.

# casa_apuestas
- Detecta por logo, color o footer. Ej: Bet365 (verde oscuro), Bwin (rojo), William Hill (azul), Betfair (amarillo), Pinnacle, Stake, 1xBet, Marathonbet, Sportium, Codere.
- Si no se identifica: "desconocida".

# legs — una entrada por mercado del boleto

## jugador_1, jugador_2
- Tal cual aparecen. Dobles: separa parejas con " / ". Si no se ve el rival: "?".

## torneo
- Solo si es visible. null si no.

## fecha_evento
- Solo si aparece explícita en el boleto, formato YYYY-MM-DD. NO uses la fecha del mensaje. null si no.

## mercado (enum estricto)
- "moneyline": ganador (Match Winner, 1X2, Vincente, Money Line).
- "handicap_games": hándicap de juegos (AH -3.5, +4.5 games, Juegos Handicap…).
- "handicap_sets": hándicap de sets (-1.5 sets…).
- "over_under_games": total de juegos del partido (Más de 22.5 games, Under 21.5, Total Games…).
- "over_under_sets": total de sets (Más de 2.5 sets…).
- "set_betting": marcador exacto en sets (2-0, 2-1, 1-2, 0-2).
- "first_set_winner": ganador del primer set (1st Set Winner, Ganador 1er Set). seleccion = nombre del jugador.
- "over_under_games_set1": total de juegos en el primer set (1st Set Total Games, O/U juegos set 1). linea = valor (ej: 10.5).
- "over_under_aces": total de aces en el partido (Total Aces O/U, Más/Menos aces). linea = valor (ej: 20.5).
- "over_under_aces_jugador": aces de un jugador concreto (Player Aces O/U, ej: "Alcaraz Total Aces"). linea = valor; seleccion = nombre del jugador.
- "tiebreak_yn": ¿habrá tie-break en el partido? (Will there be a Tiebreak?, ¿Tie-break en el partido?). seleccion = "si" o "no".
- "first_set_ou_games": sinónimo de over_under_games_set1 que usan algunas bookies.
- Si el mercado es exótico o no encaja exactamente, elige el más cercano.

## seleccion
- moneyline / handicap_games / handicap_sets / first_set_winner: nombre del jugador apostado.
- over_under_games / over_under_sets / over_under_games_set1 / first_set_ou_games / over_under_aces: "over" o "under".
- over_under_aces_jugador: nombre del jugador (ej: "Alcaraz").
- set_betting: "2-0", "2-1", "1-2" o "0-2".
- tiebreak_yn: "si" o "no".

## linea
- Handicap con signo: -3.5, +1.5.
- Totales positivos: juegos 22.5, sets 2.5, juegos set1 10.5, aces 20.5.
- null para moneyline, set_betting, first_set_winner y tiebreak_yn.

# cuota_total — cuota final del boleto (>=1.0).
# stake_indicado — solo si aparece explícito ("1u", "0.5u", "stake 2"). null si no.

# Casos límite
- Boleto con resultado ya marcado: IGNORA la marca. Extrae solo el contenido del ticket.
- Boleto ilegible: es_pick=true, casa="desconocida", legs=[], cuota_total=1.0.

Precisión > exhaustividad. Es mejor no_verificable que inventar datos."""

# ---------------------------------------------------------------------------

FOOTBALL_PROMPT = """Eres un analista experto en boletos de apuestas de FÚTBOL publicados por tipsters en Telegram.

Tu función es extraer los datos estructurados del boleto. La precisión en equipos, competición, mercado, selección y línea es crítica. Nada de inferir resultados.

# Reglas generales
- Devuelve EXCLUSIVAMENTE el JSON conforme al esquema. Sin texto extra, sin markdown.
- Conserva el idioma original. No traduzcas nombres de equipos ni competiciones.
- sport SIEMPRE debe ser "football".

# es_pick
- true: ticket con cuota y selección visibles. false: ruido.

# casa_apuestas
- Detecta por logo/color. "desconocida" si no es claro.

# legs — una entrada por mercado del boleto

## equipo_local, equipo_visitante
- Tal cual aparecen en el boleto. Si no se distingue local/visitante, el primero que aparece es local.

## competicion
- Liga, copa o torneo si es visible (Premier League, La Liga, Champions League, Serie A, Copa del Rey…). null si no.

## fecha_evento
- Solo si aparece explícita en el boleto, formato YYYY-MM-DD. null si no.

## mercado (enum estricto)
- "1x2": resultado final a 3 vías (1, X, 2). Incluye "Full Time Result", "Match Result", "Resultado Final".
- "doble_oportunidad": 1X, X2, 12. Incluye "Double Chance".
- "ambos_marcan": BTTS (Both Teams to Score), "Ambos marcan". seleccion = "si" o "no".
- "over_under_goles": total de goles en el partido. linea = valor (ej: 2.5, 3.5).
- "over_under_goles_primera": total de goles en el primer tiempo. linea = valor.
- "handicap_asiatico": Asian Handicap, AH. linea = valor decimal (ej: -0.5, +1.5, -1.0).
- "handicap_europeo": hándicap europeo con valor entero. linea = valor (ej: +1, -2).
- "marcador_exacto": Correct Score, Marcador Exacto. seleccion = "1-0", "2-1", etc.
- "resultado_descanso_final": HT/FT, Descanso/Final. seleccion = "1/1", "X/2", "1/X", etc.
- "goles_equipo_ou": goles de un equipo específico (over/under). linea = valor.
- "primera_mitad_1x2": resultado al descanso (Half Time Result). seleccion = "1", "X" o "2".
- "tarjetas_ou": total de tarjetas. linea = valor.
- "corners_ou": total de córners. linea = valor.
- Si el mercado no encaja exactamente, elige el más cercano.

## seleccion
- 1x2: "1", "X" o "2".
- doble_oportunidad: "1X", "X2" o "12".
- ambos_marcan: "si" o "no".
- over/under (goles, corners, tarjetas, goles_equipo_ou): "over" o "under".
- handicap: nombre del equipo apostado (el que tiene la ventaja/desventaja).
- marcador_exacto: "1-0", "2-1", "0-0", etc.
- resultado_descanso_final: "1/1", "X/2", "1/X", "X/X", etc.
- primera_mitad_1x2: "1", "X" o "2".

## linea
- Para over/under y handicap: valor numérico. null para 1x2, doble_oportunidad, ambos_marcan, marcador_exacto, resultado_descanso_final, primera_mitad_1x2.

# cuota_total — cuota final del boleto (>=1.0).
# stake_indicado — solo si aparece explícito. null si no.

# Casos límite
- Boleto con resultado ya marcado: IGNORA la marca. Extrae solo el ticket.
- Boleto ilegible: es_pick=true, casa="desconocida", legs=[], cuota_total=1.0.

Precisión > exhaustividad."""

# ---------------------------------------------------------------------------

DARTS_PROMPT = """Eres un analista experto en boletos de apuestas de DARDOS publicados por tipsters en Telegram.

Tu función es extraer los datos estructurados del boleto. La precisión en jugadores, competición, mercado, selección y línea es crítica. Nada de inferir resultados.

# Reglas generales
- Devuelve EXCLUSIVAMENTE el JSON conforme al esquema. Sin texto extra, sin markdown.
- Conserva nombres tal cual aparecen (Van Gerwen, Wright, Price, Aspinall, Smith, etc.).
- sport SIEMPRE debe ser "darts".

# es_pick
- true: ticket con cuota y selección visibles. false: ruido.

# casa_apuestas
- Detecta por logo/color. "desconocida" si no es claro.

# legs — una entrada por mercado del boleto

## jugador_1, jugador_2
- Tal cual aparecen. Si no hay rival visible: "?".

## competicion
- PDC, BDO, Premier League Darts, World Championship, Grand Prix, UK Open, Masters, Players Championship… null si no se ve.

## fecha_evento
- Solo si aparece explícita en el boleto, formato YYYY-MM-DD. null si no.

## mercado (enum estricto)
- "moneyline": ganador del partido (Match Winner, To Win Match, Ganador).
- "handicap_legs": hándicap en legs. Ej: "Van Gerwen -2.5 legs", "Wright +1.5 legs". linea = valor con signo.
- "over_under_legs": total de legs jugadas. linea = valor (ej: 5.5, 7.5, 9.5).
- "set_betting": marcador exacto en sets. seleccion = "3-0", "3-1", "3-2", "2-3", "1-3", "0-3", etc.
- "180s_match": total de 180s en el partido (over/under). linea = valor (ej: 6.5, 8.5).
- "checkout_mayor": checkout más alto del partido (over/under). linea = valor (ej: 100.5, 120.5).
- "primera_pierna": ganador de la primera leg.

## seleccion
- moneyline / handicap_legs / primera_pierna: nombre del jugador apostado.
- over_under_legs / 180s_match / checkout_mayor: "over" o "under".
- set_betting: marcador exacto, ej: "3-1", "3-2", "2-3".

## linea
- handicap_legs con signo: -1.5, +2.5. Totales positivos: 5.5, 8.5, 100.5. null para moneyline, primera_pierna y set_betting.

# cuota_total — cuota final del boleto (>=1.0).
# stake_indicado — solo si aparece explícito. null si no.

# Casos límite
- Boleto con resultado ya marcado: IGNORA la marca. Extrae solo el ticket.
- Boleto ilegible: es_pick=true, casa="desconocida", legs=[], cuota_total=1.0.

Precisión > exhaustividad."""


# ---------------------------------------------------------------------------

BASKETBALL_PROMPT = """Eres un analista experto en boletos de apuestas de BALONCESTO publicados por tipsters en Telegram.

Tu función es extraer los datos estructurados del boleto. La precisión en equipos, competición, mercado, selección y línea es crítica. Nada de inferir resultados.

# Reglas generales
- Devuelve EXCLUSIVAMENTE el JSON conforme al esquema. Sin texto extra, sin markdown.
- Conserva el idioma original. No traduzcas nombres de equipos ni de jugadores.
- Mantén los prefijos cortos de ciudad si aparecen (BOS Celtics, DEN Nuggets, SA Spurs, NY Knicks, TOR Tempo, IND Fever, CHA Hornets, CHI Bulls, DET Pistons, ORL Magic, MEM Grizzlies, BKN Nets, DAL Mavericks, CLE Cavaliers, OKC Thunder…). El resolver se encarga de normalizar.
- sport SIEMPRE debe ser "basketball".

# es_pick
- true: ticket con cuota y selección visibles. false: ruido (memes, banners, capturas vacías).
- Boleto con la selección difuminada/censurada/borrosa (solo se ve el importe y el botón de cerrar): es_pick=true pero legs=[]. No inventes selecciones a partir del footer si el cuerpo está pixelado.
- Boleto con solo footer/equipos visibles pero sin mercado ni selección: es_pick=true, legs=[].

# Etiquetas a IGNORAR (no son mercados, no son ganada/perdida)
- "CASHOUT" / "Cierre" / "Cerrar apuesta": indica cobro anticipado; ignóralo, NO marca el resultado.
- "En curso" / "Ganada" / "Perdida" / checks verdes / banderines / "ACERTADA" / "Ganancias": resultado/estado del ticket; ignóralo, extrae solo el contenido apostado.
- Marcadores finales junto a los equipos (ej: "Fenerbahce 93 / Besiktas 68", "Fenerbahce 93 - 68 Besiktas"): son el resultado real, NO el mercado apostado.
- Contexto de serie / playoffs en texto libre (ej: "5º partido, NYK lidera la serie por 3-1", "Game 4, NYK leads series 2-1"): informativo, no es un mercado.
- Barras de progreso con cifras parciales (ej: "161 ── 176.5"): es solo visualización, no extraigas.
- Iconos "+20" sobre el balón: indicador de cuota mejorada del bookie, ignorar.

# Cláusula "Prórroga incluida" / "Sin prórroga"
- Si el boleto lo dice explícitamente, rellena `prorroga_incluida` en cada leg afectada (true si incluye OT, false si no). null si no se menciona.
- Es habitual en player props de NBA/Euroliga: afecta a totales y a líneas de jugador.

# MYMATCH / Bet Builder / Combinada de partido / Same Game Parlay
- Cuando el boleto agrupa 2+ mercados del MISMO partido bajo una única cuota (ej: "MYMATCH: Ganador Fenerbahce + Menos de 176,5"), extrae cada sub-mercado como UNA leg independiente.
- Todas las legs del combo comparten equipo_local/equipo_visitante y fecha. `cuota_individual = null` en cada leg (no es público). La `cuota_total` del ticket es la cuota combinada del bet builder.
- Ejemplo (Fenerbahce vs Besiktas, MYMATCH 1.78):
  - leg 1: mercado=moneyline, seleccion="Fenerbahce"
  - leg 2: mercado=over_under_puntos, seleccion="under", linea=176.5

# casa_apuestas
- Detecta por logo/color (Codere fondo oscuro + verde, Bet365 verde lima, Bwin, William Hill…). "desconocida" si no es claro.

# legs — una entrada por mercado del boleto

## equipo_local, equipo_visitante
- Tal cual aparecen en el boleto, conservando prefijos cortos.
- Si la línea muestra "EquipoA - EquipoB", EquipoA es local, EquipoB visitante.
- En NBA suele ser "Visitante @ Local"; respétalo si está claro.

## competicion
- NBA, WNBA, ACB, Euroliga, Eurocup, NCAA, BCL, FIBA, Lega Basket (Italia), BBL (Alemania), LNB Pro A (Francia), LKL (Lituania), BSL (Turquía, Basketbol Süper Ligi), LEB Oro / Primera FEB (España, 2ª división), etc. null si no se ve.
- Pistas por bandera/equipos: bandera francesa + Paris/Cholet → LNB Pro A; bandera lituana + Neptunas/Lietkabelis → LKL; bandera turca + Fenerbahce/Besiktas → BSL o Euroliga; bandera griega + Olympiakos/Panathinaikos → Euroliga.

## fecha_evento
- Solo si aparece explícita en el boleto, formato YYYY-MM-DD. null si no.
- "Hoy", "Mañana", horas sueltas: no son fechas explícitas → null.

## mercado (enum estricto)
- "moneyline": ganador del partido (Money Line, Match Winner, Ganador, "Ganador sin empate"). Sin empate.
- "handicap_puntos": spread / hándicap del partido ("Hándicap de puntos", "Point Spread"). linea = valor con signo.
- "over_under_puntos": total de puntos del PARTIDO ("Número total de puntos", "Total Points"). linea = valor.
- "over_under_puntos_equipo": total de puntos de UN equipo ("Más de 103.5 puntos para ORL Magic", "Equipo - Totales", "Team Total Points"). seleccion = equipo; over_under = "over"|"under"; linea = valor.
- "over_under_mitad": total de puntos de una mitad (1st Half Total). linea = valor. (rellena `periodo`)
- "over_under_cuarto": total de puntos de un cuarto (Q1 Total…). linea = valor. (rellena `periodo`)
- "ganador_mitad": ganador de una mitad ("1ª mitad - Ganador", "1ª mitad - Ganador sin empate"). seleccion = equipo. (rellena `periodo`)
- "ganador_cuarto": ganador de un cuarto. seleccion = equipo. (rellena `periodo`)
- "handicap_mitad": hándicap de una mitad. linea = valor con signo. (rellena `periodo`)
- "puntos_jugador": Puntos de un jugador ("Jared McCain - Más de 11.5", "Banchero 5+ puntos").
- "rebotes_jugador": Rebotes de un jugador ("Mitchell Robinson - Más de 7.5").
- "asistencias_jugador": Asistencias de un jugador ("Nicolas Claxton - Más de 3.5", "Jokic 10+ asistencias").
- "triples_jugador": Triples anotados por un jugador (3PT Made).
- "asistencias_rebotes_jugador": A+R combinados ("Josh Hart - Más de 12.5 Asistencias y rebotes").
- "puntos_rebotes_jugador": P+R combinados ("Pts + Reb").
- "puntos_asistencias_jugador": P+A combinados ("Pts + Ast").
- "puntos_rebotes_asistencias_jugador": PRA combinados ("Noah Penda - Menos de 11.5 puntos, asistencias y rebotes (combinados)", "Puntos, asistencias y rebotes - Más de/Menos de").
- "doble_doble_jugador": doble-doble. over_under="over"(sí) / "under"(no).
- "triple_doble_jugador": triple-doble. over_under="over"(sí) / "under"(no).
- "race_to_puntos": primer equipo en llegar a X puntos (Race to 20). seleccion = equipo; linea = X.
- Player props acotadas a un periodo concreto (ej: "Banchero 5+ puntos - 1º cuarto"): mantén el mercado base (puntos_jugador) y rellena `periodo` con "Q1".
- Si el mercado no encaja exactamente, elige el más cercano.

## Formato "X+" sin línea decimal (MUY común en player props)
- "10+ asistencias" → mercado=asistencias_jugador, linea=9.5, over_under="over".
- "5+ puntos" → mercado=puntos_jugador, linea=4.5, over_under="over".
- "Más de 12.5 asistencias y rebotes" → mercado=asistencias_rebotes_jugador, linea=12.5, over_under="over".
- "Menos de 3.5 asistencias" → mercado=asistencias_jugador, linea=3.5, over_under="under".
- Regla general: si la apuesta dice "X+" (entero), normaliza siempre a linea=X-0.5 con over_under="over".

## seleccion
- moneyline / ganador_mitad / ganador_cuarto / race_to_puntos / over_under_puntos_equipo: nombre del equipo apostado.
- handicap_puntos / handicap_mitad: nombre del equipo apostado (la línea va en `linea`).
- over_under_puntos / over_under_mitad / over_under_cuarto: "over" o "under".
- Cualquier *_jugador (incluye combos A+R, P+R, P+A, PRA): nombre del jugador.
- doble_doble / triple_doble_jugador: nombre del jugador.

## linea
- handicap con signo: -5.5, +7.5.
- Totales positivos: puntos partido 210.5, team total 103.5, mitad 105.5, cuarto 55.5.
- Player props: puntos 24.5, rebotes 9.5, asistencias 6.5, triples 2.5, combos A+R 12.5, PRA 35.5.
- race_to_puntos: el valor X (ej: 20).
- null para moneyline, ganador_mitad, ganador_cuarto, doble_doble y triple_doble.

## over_under
- Para puntos/rebotes/asistencias/triples_jugador, combos de jugador y over_under_puntos_equipo: "over" o "under".
- Para doble_doble / triple_doble_jugador: "over" si se apuesta SÍ ocurre, "under" si NO.
- null para mercados de equipo donde basta seleccion + linea, y para over_under_puntos/mitad/cuarto (donde el over/under va en `seleccion`).

## periodo
- "Q1"/"Q2"/"Q3"/"Q4" para cuartos, "H1"/"H2" para mitades, "OT" para prórroga aislada, "full" si se indica explícitamente "partido completo".
- null cuando la apuesta se refiere al partido completo de forma implícita (caso por defecto).
- Rellénalo siempre que el boleto mencione "1º cuarto", "Q1", "2ª mitad", "1ª mitad", "Half", etc.

## prorroga_incluida
- true / false solo si lo indica explícitamente el boleto ("Prórroga incluida", "OT included", "Sin prórroga"). null en caso contrario.

# cuota_total — cuota final del boleto (>=1.0). En MYMATCH/Bet Builder es la cuota combinada del bet builder.
# stake_indicado — solo si aparece explícito (€, "u", stake). null si no.

# Casos límite
- Boleto con resultado ya marcado (Ganada/Cashout/check verde): IGNORA la marca. Extrae solo el ticket.
- Boleto ilegible / censurado / pixelado: es_pick=true, casa="desconocida", legs=[], cuota_total=1.0.

Precisión > exhaustividad."""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

_DISCARD_PAYLOAD = TennisBetPayload(
    sport="tennis",
    casa_apuestas="desconocida",
    cuota_total=1.0,
    es_pick=False,
)


class VisionExtractor:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 fallback_model: str | None = FALLBACK_MODEL) -> None:
        self._client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self._model = model
        self._fallback_model = fallback_model

    # ------------------------------------------------------------------
    # Fase 1: detección rápida de deporte
    # ------------------------------------------------------------------

    async def _detect(self, b64: str, media_type: str) -> SportDetection:
        message = await self._client.messages.parse(
            model=self._model,
            max_tokens=256,
            system=[
                {
                    "type": "text",
                    "text": DETECTION_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {"type": "text", "text": "Clasifica esta imagen."},
                    ],
                }
            ],
            output_format=SportDetection,
        )
        await asyncio.sleep(LLM_PAUSE_SECONDS)
        return message.parsed_output

    # ------------------------------------------------------------------
    # Fase 2: extracción especializada
    # ------------------------------------------------------------------

    async def _extract_typed(
        self,
        b64: str,
        media_type: str,
        prompt: str,
        output_model: type,
        model: str | None = None,
    ):
        message = await self._client.messages.parse(
            model=model or self._model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {"type": "text", "text": "Extrae el boleto siguiendo estrictamente el esquema JSON."},
                    ],
                }
            ],
            output_format=output_model,
        )
        await asyncio.sleep(LLM_PAUSE_SECONDS)
        return message.parsed_output

    async def _extract_with_fallback(
        self, b64: str, media_type: str, prompt: str, output_model: type,
    ):
        """Extrae con el modelo barato; si dice es_pick=true pero legs=[],
        reintenta con el modelo potente. Casi todas las imágenes salen al primer
        intento; el fallback solo dispara en tickets que haiku no supo leer."""
        payload = await self._extract_typed(b64, media_type, prompt, output_model)
        if (
            self._fallback_model
            and getattr(payload, "es_pick", False)
            and not getattr(payload, "legs", None)
        ):
            log.info("Vision haiku devolvió legs=[]; reintento con %s", self._fallback_model)
            retry = await self._extract_typed(
                b64, media_type, prompt, output_model, model=self._fallback_model,
            )
            if getattr(retry, "legs", None):
                return retry
        return payload

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def extract(self, image_bytes: bytes, media_type: str = "image/jpeg") -> AnyBetPayload:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")

        detection = await self._detect(b64, media_type)
        log.debug("Detección: es_pick=%s sport=%s", detection.es_pick, detection.sport)

        if not detection.es_pick:
            return _DISCARD_PAYLOAD

        if detection.sport == "tennis":
            return await self._extract_with_fallback(b64, media_type, TENNIS_PROMPT, TennisBetPayload)
        if detection.sport == "football":
            return await self._extract_with_fallback(b64, media_type, FOOTBALL_PROMPT, FootballBetPayload)
        if detection.sport == "darts":
            return await self._extract_with_fallback(b64, media_type, DARTS_PROMPT, DartsBetPayload)
        if detection.sport == "basketball":
            return await self._extract_with_fallback(b64, media_type, BASKETBALL_PROMPT, BasketballBetPayload)

        log.warning("Deporte no soportado detectado: %s — descartando imagen.", detection.sport)
        return _DISCARD_PAYLOAD
