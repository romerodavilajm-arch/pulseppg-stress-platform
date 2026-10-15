"""Consultas de la API: sesiones del esquema central y resultados del Analyzer HRV.

La API solo lee. De `sessions`/`devices` obtiene los metadatos de la captura y
del esquema `hrv` (propiedad del Analyzer, ver analyzer/schema.sql) los
resultados disponibles. No escribe en ninguna tabla (docs/flujo.md §1, §8-9).

Estado del análisis de una sesión, derivado aquí y no guardado en la base
(docs/flujo.md §7):
  - completed: hay fila en hrv.stress_results.
  - failed:    el Analyzer la dio por imposible (fallo permanente o agotó
               sus HRV_MAX_ATTEMPTS intentos).
  - pending:   ni lo uno ni lo otro, incluido el caso en que el Analyzer aún
               no ha creado su esquema.

En las sesiones low_quality el Analyzer guarda valores sin sentido para las
métricas HRV, así que nunca se devuelven. La vista de administración solo
recibe las de calidad (sqi, perfusion_index, snr_db), que explican el
descarte (docs/flujo.md §8.6).
"""

import os
import re
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

MAX_ATTEMPTS = int(os.environ.get("HRV_MAX_ATTEMPTS", "3"))
TIMEZONE = os.environ.get("API_TIMEZONE", "America/Mexico_City")
ADMIN_LIMIT = int(os.environ.get("API_ADMIN_LIMIT", "500"))

CODE_RE = re.compile(r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$")
LABELS = ("stress", "no_stress", "low_quality")
STATUSES = ("pending", "failed")
QUALITY_METRICS = ("sqi", "perfusion_index", "snr_db")

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, "")
    return _pool


@contextmanager
def cursor():
    pool = _get_pool()
    conn = pool.getconn()
    broken = False
    try:
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    except psycopg2.Error:
        broken = True
        raise
    finally:
        pool.putconn(conn, close=broken)


def normalize_code(raw):
    """Código tal como lo escribe el usuario -> código válido o None."""
    code = re.sub(r"[\s-]", "", raw or "").upper()
    return code if CODE_RE.match(code) else None


def _hrv_ready(cur):
    cur.execute("SELECT to_regclass('hrv.stress_results') IS NOT NULL"
                " AND to_regclass('hrv.failures') IS NOT NULL"
                " AND to_regclass('hrv.metrics') IS NOT NULL AS ok")
    return cur.fetchone()["ok"]


def _session_sql(hrv_ready, where):
    if hrv_ready:
        result_cols = """
            CASE WHEN r.session_id IS NOT NULL THEN 'completed'
                 WHEN f.permanent OR f.attempts >= %(max_attempts)s THEN 'failed'
                 ELSE 'pending' END AS status,
            r.label, r.score::float AS score, r.rules_met, r.computed_at,
            f.error AS failure_error, f.attempts AS failure_attempts"""
        joins = """
            LEFT JOIN hrv.stress_results r ON r.session_id = s.id
            LEFT JOIN hrv.failures f ON f.session_id = s.id"""
    else:
        result_cols = """
            'pending' AS status, NULL::text AS label, NULL::float AS score,
            NULL::smallint AS rules_met, NULL::timestamptz AS computed_at,
            NULL::text AS failure_error, NULL::int AS failure_attempts"""
        joins = ""
    return f"""
        SELECT * FROM (
            SELECT s.id, s.session_code, d.device_key, s.started_at, s.finished_at,
                   EXTRACT(EPOCH FROM s.finished_at - s.started_at)::float AS duration_seconds,
                   s.sample_count, s.file_hash, s.created_at, {result_cols}
            FROM sessions s
            JOIN devices d ON d.id = s.device_id {joins}
        ) q
        WHERE {where}
    """


def _metrics(cur, session_id, label, admin):
    if label is None:
        return None
    if label == "low_quality" and not admin:
        return None
    cur.execute("SELECT metric, value FROM hrv.metrics WHERE session_id = %s ORDER BY metric",
                (session_id,))
    metrics = {r["metric"]: r["value"] for r in cur.fetchall()}
    if label == "low_quality":
        metrics = {k: v for k, v in metrics.items() if k in QUALITY_METRICS}
    return metrics


def _analysis(row, metrics, admin):
    out = {
        "analyzer": "hrv",
        "status": row["status"],
        "label": row["label"],
        "score": row["score"],
        "rules_met": row["rules_met"],
        "computed_at": row["computed_at"],
        "metrics": metrics,
    }
    if admin and row["status"] != "completed" and row["failure_error"]:
        out["failure"] = {"error": row["failure_error"], "attempts": row["failure_attempts"]}
    return out


def session_by_code(code):
    """Vista pública: sesión por código o None si no existe."""
    with cursor() as cur:
        ready = _hrv_ready(cur)
        cur.execute(_session_sql(ready, "session_code = %(code)s"),
                    {"code": code, "max_attempts": MAX_ATTEMPTS})
        row = cur.fetchone()
        if row is None:
            return None
        metrics = _metrics(cur, row["id"], row["label"], admin=False)
    return {
        "session_code": row["session_code"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_seconds": row["duration_seconds"],
        "sample_count": row["sample_count"],
        "analysis": _analysis(row, metrics, admin=False),
    }


def _admin_row(row):
    return {
        "id": row["id"],
        "session_code": row["session_code"],
        "device_key": row["device_key"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_seconds": row["duration_seconds"],
        "sample_count": row["sample_count"],
        "created_at": row["created_at"],
        "status": row["status"],
        "label": row["label"],
        "score": row["score"],
    }


def admin_sessions(device=None, date_from=None, date_to=None, result=None):
    """Tabla global. Filtros: device_key, fechas (días en TIMEZONE, ambos
    inclusive) y resultado (una etiqueta, 'pending' o 'failed')."""
    where = ["TRUE"]
    params = {"max_attempts": MAX_ATTEMPTS, "tz": TIMEZONE, "limit": ADMIN_LIMIT}
    if device:
        where.append("device_key = %(device)s")
        params["device"] = device
    if date_from:
        where.append("started_at >= (%(date_from)s::date)::timestamp AT TIME ZONE %(tz)s")
        params["date_from"] = date_from
    if date_to:
        where.append("started_at < (%(date_to)s::date + 1)::timestamp AT TIME ZONE %(tz)s")
        params["date_to"] = date_to
    if result in LABELS:
        where.append("label = %(result)s")
        params["result"] = result
    elif result in STATUSES:
        where.append("status = %(result)s")
        params["result"] = result
    with cursor() as cur:
        ready = _hrv_ready(cur)
        cur.execute(_session_sql(ready, " AND ".join(where))
                    + " ORDER BY started_at DESC, id DESC LIMIT %(limit)s", params)
        rows = [_admin_row(r) for r in cur.fetchall()]
        cur.execute("SELECT device_key FROM devices ORDER BY device_key")
        devices = [r["device_key"] for r in cur.fetchall()]
    return rows, devices


def admin_session(session_id):
    with cursor() as cur:
        ready = _hrv_ready(cur)
        cur.execute(_session_sql(ready, "id = %(id)s"),
                    {"id": session_id, "max_attempts": MAX_ATTEMPTS})
        row = cur.fetchone()
        if row is None:
            return None
        metrics = _metrics(cur, row["id"], row["label"], admin=True)
    out = _admin_row(row)
    out["file_hash"] = row["file_hash"]
    out["analysis"] = _analysis(row, metrics, admin=True)
    return out


def ping():
    with cursor() as cur:
        cur.execute("SELECT 1")
