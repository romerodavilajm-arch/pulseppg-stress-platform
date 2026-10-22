"""PulsePPG API: endpoints JSON y páginas HTML para el usuario y el administrador.

Solo consulta: no participa en la captura ni en el procesamiento
(docs/flujo.md §1, §8-9). Las consultas viven en db.py.

Usuario (sin caducidad, por código de sesión):
  GET /                       formulario "Consultar"
  GET /consulta?code=XXXXXX   redirige a /sesion/XXXXXX
  GET /sesion/{code}          resultado
  GET /api/sessions/{code}    JSON

Administración (sin autenticación en el prototipo):
  GET /admin                  tabla global con filtros device, date_from, date_to, result
  GET /admin/sesiones/{id}    detalle
  GET /api/admin/sessions     JSON de la tabla, mismos filtros
  GET /api/admin/sessions/{id}

GET /health comprueba la conexión con PostgreSQL.
GET /static/...              Bootstrap (sin CDN, para funcionar sin internet)
"""

import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db

log = logging.getLogger("api")

NOT_FOUND = "Código no encontrado"
TZ = ZoneInfo(db.TIMEZONE)

# Segundos entre recargas de /sesion/{code} mientras el análisis está pendiente.
REFRESH_SECONDS = 10

LABEL_TEXT = {"stress": "Estrés", "no_stress": "Sin estrés", "low_quality": "Baja calidad"}
STATUS_TEXT = {"pending": "Análisis pendiente", "failed": "Análisis fallido"}
RESULT_OPTIONS = [("stress", "Estrés"), ("no_stress", "Sin estrés"),
                  ("low_quality", "Baja calidad"), ("pending", "Pendiente"),
                  ("failed", "Fallido")]
# Métricas mostradas al usuario (docs/flujo.md §8.6): clave, nombre, unidad, decimales.
USER_METRICS = [("bpm", "Frecuencia cardíaca", "lpm", 0),
                ("sdnn", "SDNN", "ms", 1),
                ("rmssd", "RMSSD", "ms", 1),
                ("pnn50", "pNN50", "%", 1),
                ("lf_hf_ratio", "Ratio LF/HF", "", 2),
                ("respiratory_rate", "Frecuencia respiratoria", "rpm", 1)]

app = FastAPI(title="PulsePPG Stress Platform", docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
# Bootstrap se sirve desde la propia API para que la web funcione sin internet.
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")


def localtime(value):
    if value is None:
        return "—"
    return value.astimezone(TZ).strftime("%d/%m/%Y %H:%M")


def duration(seconds):
    if seconds is None:
        return "—"
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d} min"


def number(value, decimals=2):
    return "—" if value is None else f"{value:.{decimals}f}"


templates.env.filters.update(localtime=localtime, duration=duration, number=number)
templates.env.globals.update(LABEL_TEXT=LABEL_TEXT, STATUS_TEXT=STATUS_TEXT,
                             USER_METRICS=USER_METRICS, TIMEZONE=db.TIMEZONE,
                             REFRESH_SECONDS=REFRESH_SECONDS)


def render(request, name, status_code=200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)


@app.exception_handler(psycopg2.Error)
def database_error(request: Request, exc: psycopg2.Error):
    # docs/flujo.md §15: fallo de PostgreSQL -> 500 genérico.
    log.error("Error de base de datos en %s: %s", request.url.path, exc)
    if request.url.path.startswith("/api/") or request.url.path == "/health":
        return JSONResponse({"detail": "Error interno"}, status_code=500)
    return render(request, "error.html", 500, message="Error interno. Inténtalo más tarde.")


# ---------- Usuario ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render(request, "index.html")


@app.get("/consulta")
def lookup(request: Request, code: str = ""):
    normalized = db.normalize_code(code)
    if normalized is None:
        return render(request, "index.html", 404, code=code, error=NOT_FOUND)
    return RedirectResponse(f"/sesion/{normalized}", status_code=303)


@app.get("/sesion/{code}", response_class=HTMLResponse)
def session_page(request: Request, code: str):
    normalized = db.normalize_code(code)
    session = db.session_by_code(normalized) if normalized else None
    if session is None:
        return render(request, "index.html", 404, code=code, error=NOT_FOUND)
    return render(request, "session.html", session=session)


@app.get("/api/sessions/{code}")
def session_json(code: str):
    normalized = db.normalize_code(code)
    session = db.session_by_code(normalized) if normalized else None
    if session is None:
        raise HTTPException(404, NOT_FOUND)
    return session


# ---------- Administración ----------

def _filters(device, date_from, date_to, result):
    def parse_date(value, name):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"Fecha no válida en {name}: {value!r} (formato AAAA-MM-DD)")

    if result and result not in db.LABELS + db.STATUSES:
        raise HTTPException(400, f"Resultado no válido: {result!r}")
    return {"device": device or None,
            "date_from": parse_date(date_from, "date_from"),
            "date_to": parse_date(date_to, "date_to"),
            "result": result or None}


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, device: str = "", date_from: str = "", date_to: str = "",
          result: str = ""):
    filters = _filters(device, date_from, date_to, result)
    rows, devices = db.admin_sessions(**filters)
    return render(request, "admin.html", rows=rows, devices=devices, filters=filters,
                  result_options=RESULT_OPTIONS, limit=db.ADMIN_LIMIT,
                  now=datetime.now(TZ))


@app.get("/admin/sesiones/{session_id}", response_class=HTMLResponse)
def admin_detail(request: Request, session_id: int):
    session = db.admin_session(session_id)
    if session is None:
        return render(request, "error.html", 404, message="Sesión no encontrada")
    return render(request, "admin_session.html", session=session)


@app.get("/api/admin/sessions")
def admin_json(device: str = "", date_from: str = "", date_to: str = "", result: str = ""):
    rows, _ = db.admin_sessions(**_filters(device, date_from, date_to, result))
    return {"sessions": rows, "limit": db.ADMIN_LIMIT}


@app.get("/api/admin/sessions/{session_id}")
def admin_detail_json(session_id: int):
    session = db.admin_session(session_id)
    if session is None:
        raise HTTPException(404, "Sesión no encontrada")
    return session


@app.get("/health")
def health():
    db.ping()
    return {"status": "ok"}
