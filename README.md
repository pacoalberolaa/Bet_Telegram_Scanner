# BetTelegramScanner

Pipeline automático para auditar el histórico de apuestas de un tipster de Telegram.
Extrae los boletos publicados como imágenes, los normaliza a JSON estructurado,
verifica el resultado real contra fuentes externas y calcula el ROI.

## Idea

Los tipsters de apuestas publican sus picks en canales de Telegram, casi siempre como
**capturas de los boletos** (Bet365, Bwin, Pinnacle…). No hay forma estándar de auditar
si esos picks ganaron o perdieron: las imágenes son ruido para cualquier sistema, y
revisarlas a mano es inviable a partir de unos pocos cientos.

BetTelegramScanner automatiza ese trabajo:

1. **Ingiere** el historial completo de un canal (export oficial de Telegram Desktop).
2. **Filtra** mensajes que no son boletos (reenvíos, replies, banners, memes).
3. **Detecta el deporte** de cada boleto con un LLM de visión (Claude Haiku 4.5).
4. **Extrae** los datos estructurados (jugadores, mercado, cuota, selección, línea)
   con un prompt especializado por deporte.
5. **Verifica** el resultado real contra Tennis Explorer (tenis) o fuentes equivalentes
   (fútbol, dardos).
6. **Calcula el ROI** unitario y emite un Excel con todos los picks y su resolución.

Todo persistido en MongoDB Atlas, idempotente (re-ejecutable sin duplicar trabajo) y
paralelizado por meses para procesar años de histórico en minutos.

## Arquitectura

```
                    ┌──────────────────────────────┐
                    │  Telegram Desktop Export     │
                    │  (carpeta ChatExport_*.json) │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                  ┌─────────────────────────────────┐
                  │  ingest_export.ExportIngest     │
                  │  - filtra type=message          │
                  │  - descarta fwd / replies       │
                  │  - exige campo "photo"          │
                  │  - normaliza fecha a UTC        │
                  └──────────────┬──────────────────┘
                                 │  MessageCandidate
                                 ▼
                  ┌─────────────────────────────────┐
                  │  pipeline.process_candidate     │
                  │  ├── exists? → skip             │
                  │  ├── pHash visual               │
                  │  ├── dedup ventana 72h          │
                  │  ├── vision.extract (2 fases)   │
                  │  └── resolver_<sport>           │
                  └──────────────┬──────────────────┘
                                 │  PickDocument
                                 ▼
                  ┌─────────────────────────────────┐
                  │  MongoDB Atlas                  │
                  │  - colección picks              │
                  │  - colección te_matches (cache) │
                  └──────────────┬──────────────────┘
                                 │
                                 ▼
                  ┌─────────────────────────────────┐
                  │  analytics + reports            │
                  │  → reports/<tipster>_*.xlsx     │
                  └─────────────────────────────────┘
```

### Flujo de ejecución

`python -m bettelegramscanner` (`__main__.py`):

1. `cli.collect_request()` — pregunta por terminal carpeta + tipster + rango de fechas
   (máximo 6 meses por ejecución, `cli.py:10`).
2. `month_chunks()` parte la ventana en trozos mensuales.
3. `asyncio.Semaphore(WORKERS)` lanza N workers; cada uno procesa un mes en paralelo.
4. Por cada candidato del mes: `pipeline.process_candidate()`.
5. Al terminar todos los chunks: `build_report()` + `export_xlsx()`.

## Componentes

| Módulo | Responsabilidad |
|---|---|
| `__main__.py` | Entry point. Orquesta workers, ejecuta `_run_chunks` y emite el Excel final. |
| `cli.py` | Recoge inputs por terminal (carpeta del export, tipster, fechas). |
| `config.py` | Constantes y carga de env vars (workers, rate limits, dedup, output). |
| `ingest_export.py` | Lee `result.json` del export de Telegram, filtra mensajes válidos. |
| `dedup.py` | Calcula pHash visual (`imagehash`) y busca duplicados en ventana 72h. |
| `vision.py` | Cliente Anthropic. Fase 1: detección de deporte. Fase 2: extracción especializada con prompt-cache. |
| `models.py` | Esquemas Pydantic (Tennis/Football/Darts payloads + PickDocument + PickResolution). |
| `pipeline.py` | Orquesta por candidato: exists → pHash → dedup → vision → resolver → persist. |
| `resolver_tennis.py` | Verifica resultados contra Tennis Explorer (scraping + cache en Mongo). |
| `resolver_football.py` | Verifica resultados de fútbol (stub/placeholder a ampliar). |
| `resolver_darts.py` | Verifica resultados de dardos (stub/placeholder a ampliar). |
| `storage.py` | Capa Mongo async (Motor). Índices únicos por (tipster, message_id). |
| `analytics.py` | Calcula `profit_units` y construye el `Report`. |
| `reports.py` | Exporta el reporte y los picks a Excel (`openpyxl`). |

## Stack técnico

- **Python 3.12+** con `asyncio`
- **Anthropic SDK** — Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) para visión + extracción estructurada
- **Motor / PyMongo** — MongoDB Atlas async
- **Pydantic v2** — validación de esquemas y `messages.parse()` con `output_format`
- **imagehash + Pillow** — pHash perceptual para dedup
- **httpx + BeautifulSoup + RapidFuzz** — scraping y fuzzy matching de Tennis Explorer
- **openpyxl** — generación de Excel
- **python-dotenv** — carga de `.env`

## Decisiones de diseño

### Por qué dos fases en la extracción (vision.py)
1. **Fase 1 (detección)**: clasifica la imagen como pick/no-pick + deporte. Prompt corto,
   ~300 tokens. Filtra ruido sin gastar tokens del prompt completo.
2. **Fase 2 (extracción)**: solo se ejecuta si es pick. Prompt largo y especializado
   por deporte (~1.500 tokens) con `cache_control: ephemeral` para reaprovechar el cache
   entre llamadas consecutivas.

Resultado: el ruido del canal (banners, memes, stats) cuesta solo la fase 1 (~$0.002),
mientras que los boletos válidos pagan también la fase 2 (~$0.006).

### Por qué Haiku 4.5 y no Sonnet/Opus
Para extracción estructurada de visión con esquema Pydantic definido, Haiku 4.5 da la
misma calidad práctica a **1/15 del coste de Opus** y **1/3 del coste de Sonnet**. Los
prompts ya guían explícitamente el formato; no se necesita razonamiento complejo.

### Por qué dedup visual con pHash y no por hash exacto
Los tipsters reenvían sus picks con frecuencia (a veces con recortes o reescalados
mínimos). El pHash perceptual (`imagehash.phash`) captura esos casi-duplicados con
distancia de Hamming configurable (`PHASH_HAMMING_MAX=8` por defecto). Reduce
~15-25% de llamadas a la API.

### Por qué partir por meses y no por días
Los tipsters publican en oleadas; partir por días desperdicia workers. Los meses dan
~300-1.000 boletos por chunk, suficiente para amortizar el overhead de cada worker
async y mantener cache del prompt caliente.

### Idempotencia
El índice único `(tipster, message_id)` en `picks` permite re-ejecutar el pipeline
sobre el mismo rango sin duplicar trabajo. Si un boleto ya está persistido, el pipeline
lo salta antes de llamar a la API.

## Setup

### Requisitos previos
- Cuenta Anthropic con API key — https://console.anthropic.com (incluye $5 de crédito gratis al registrarse)
- Cluster MongoDB Atlas M0 (gratis para siempre) — https://cloud.mongodb.com
- Telegram Desktop instalado — https://desktop.telegram.org

### Instalación

```bash
git clone <repo>
cd Bet_Telegram_Scanner
pip install -r requirements.txt
cp .env.example .env
# editar .env con tus credenciales reales
```

### Variables de entorno (`.env`)

| Variable | Obligatoria | Descripción |
|---|---|---|
| `MONGO_URI` | ✅ | URI de conexión a MongoDB Atlas (incluye usuario y password) |
| `MONGO_DB` | ✅ | Nombre de la base de datos (la crea Mongo al primer insert) |
| `ANTHROPIC_API_KEY` | ✅ | API key de console.anthropic.com (`sk-ant-…`) |
| `BETTELEGRAMSCANNER_WORKERS` | ❌ | Workers concurrentes (default 3, baja a 1 si tienes tier free) |
| `BETTELEGRAMSCANNER_DEDUP_WINDOW_HOURS` | ❌ | Ventana de dedup visual (default 72h) |
| `BETTELEGRAMSCANNER_PHASH_HAMMING_MAX` | ❌ | Distancia de Hamming máxima para considerar duplicado (default 8) |
| `BETTELEGRAMSCANNER_TE_RATE_SECONDS` | ❌ | Rate limit del scraper de Tennis Explorer (default 2.5s) |
| `BETTELEGRAMSCANNER_REPORTS_DIR` | ❌ | Carpeta de salida de los Excel (default `reports/`) |

⚠️ Antes de conectar a Atlas, añade tu IP en **Security → Network Access** o el cluster rechazará la conexión.

## Uso

### 1. Exportar un canal de Telegram

Telegram Desktop → abre el canal → ⋮ → **Export chat history**:

- ✅ **Photos** (imprescindible)
- ❌ Todo lo demás
- Format: **Machine-readable JSON**
- Size limit: 32 MB
- From / To: el rango que quieras analizar

Resultado: una carpeta `ChatExport_YYYY-MM-DD/` con `result.json` y subcarpeta `photos/`.

### 2. Lanzar el pipeline

```bash
python -m bettelegramscanner
```

Pregunta interactivamente:
- **Carpeta del export**: ruta absoluta del `ChatExport_…`
- **Etiqueta del tipster**: nombre lógico (sugerido el del canal)
- **Fecha inicio / fin**: rango a procesar (máximo 6 meses)

Salida:
- Documentos persistidos en `MongoDB → <MONGO_DB> → picks`
- Excel en `reports/<tipster>_<inicio>_<fin>_<timestamp>.xlsx`

### 3. Re-ejecutar / extender
Re-lanzar la misma ventana es seguro (no duplica). Para ampliar la cobertura: vuelve a
exportar Telegram con un rango mayor y vuelve a lanzar — solo procesará los boletos nuevos.

## Estimación de coste

Modelo: **Claude Haiku 4.5** ($1/MTok input · $5/MTok output).

| Escenario | Imágenes/día | Válidas/día | Coste/mes | Coste/año |
|---|---|---|---|---|
| Bajo | 1-2 | <1 | < $0.20 | ~$2 |
| Medio | 5-7 | 2-3 | ~$0.80 | ~$10 |
| Alto | 10-15 | 3-5 | ~$1.20 | ~$15 |

Los **$5 de crédito gratis** de Anthropic alcanzan típicamente para **6-12 meses de un canal medio**.

## Limitaciones conocidas

- Los resolvers de **fútbol y dardos** son placeholders. Tennis está completo vía Tennis Explorer.
- El máximo de **6 meses por ejecución** es un hard-cap defensivo (`cli.py:10`). Para más, encadena ejecuciones.
- El dedup visual opera dentro de **un mismo tipster**. Reenvíos cruzados entre canales no se detectan.
- La extracción asume que el boleto es **legible**. Capturas borrosas o muy comprimidas pueden devolver `es_pick=true` con `legs=[]`.
- Telegram Desktop puede aplicar un **delay de 24h** la primera vez que exportas un canal grande (protección anti-scraping).

## Estructura del repositorio

```
Bet_Telegram_Scanner/
├── bettelegramscanner/      # paquete principal
│   ├── __main__.py          # entry point (python -m bettelegramscanner)
│   ├── cli.py               # inputs interactivos
│   ├── config.py            # constantes y env vars
│   ├── ingest_export.py     # lectura del export de Telegram
│   ├── dedup.py             # pHash + búsqueda de duplicados
│   ├── vision.py            # cliente Anthropic (detección + extracción)
│   ├── models.py            # esquemas Pydantic
│   ├── pipeline.py          # orquestación por candidato
│   ├── resolver_tennis.py   # verificación contra Tennis Explorer
│   ├── resolver_football.py # placeholder
│   ├── resolver_darts.py    # placeholder
│   ├── storage.py           # capa Mongo async
│   ├── analytics.py         # cálculo de ROI + Report
│   └── reports.py           # exportación a Excel
├── Tipster_a_Analizar/      # carpeta donde dejar exports (gitignored)
├── reports/                 # salidas Excel (gitignored)
├── requirements.txt
├── .env.example
└── README.md
```
