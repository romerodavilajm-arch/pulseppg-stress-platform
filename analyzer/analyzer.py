"""PulsePPG Analyzer HRV.

Descubre sesiones pendientes, lee sus muestras de raw_ppg, ejecuta el
pipeline de pipeline.py y guarda los resultados en su propio esquema `hrv`
(schema.sql). No crea ni modifica sesiones, dispositivos ni muestras, y no
mantiene ningún estado en el esquema central (docs/flujo.md §5-6).

Descubrimiento (propio de este Analyzer):
  - MQTT: el aviso del Ingestor en analysis/{session_id}/request.
  - Sondeo: cada POLL_SECONDS busca sesiones sin resultado en hrv.stress_results,
    lo que cubre avisos perdidos y las sesiones guardadas con el Analyzer parado.

Al terminar publica analysis/{session_id}/complete con label y score.

Configuración por variables de entorno: MQTT_HOST, MQTT_PORT, POLL_SECONDS,
MAX_ATTEMPTS, los umbrales de calidad de pipeline.py (HRV_SQI_MIN, HRV_PI_MIN,
HRV_SNR_MIN_DB) y las variables estándar de libpq (PGHOST, PGDATABASE, ...).
"""

import json
import logging
import os
import queue
import time
from pathlib import Path

import pandas as pd
import paho.mqtt.client as mqtt
import psycopg2

import pipeline

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
CLIENT_ID = "pulseppg-analyzer-hrv"
QOS = 1

SCHEMA = Path(__file__).with_name("schema.sql")

log = logging.getLogger("analyzer")

# Sesiones sin resultado y que no han agotado sus intentos.
PENDING_SQL = """
    SELECT s.id
    FROM sessions s
    LEFT JOIN hrv.failures f ON f.session_id = s.id
    WHERE NOT EXISTS (SELECT 1 FROM hrv.stress_results r WHERE r.session_id = s.id)
      AND (f.session_id IS NULL OR (NOT f.permanent AND f.attempts < %(max)s))
"""


class Database:
    def __init__(self):
        self.conn = None

    def _connection(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect("")
        return self.conn

    def reset(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except psycopg2.Error:
                pass
        self.conn = None

    def ensure_schema(self):
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA.read_text())

    def pending_sessions(self, session_id=None):
        sql = PENDING_SQL + (" AND s.id = %(id)s" if session_id is not None else "") + " ORDER BY s.id"
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, {"max": MAX_ATTEMPTS, "id": session_id})
            return [r[0] for r in cur.fetchall()]

    def load_samples(self, session_id):
        """Fase 1 (§5.2): muestras de la sesión ordenadas por tiempo. Solo lectura."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT time, ir, red FROM raw_ppg WHERE session_id = %s ORDER BY time ASC",
                        (session_id,))
            return pd.DataFrame(cur.fetchall(), columns=["time", "ir", "red"])

    def save_result(self, session_id, result):
        """Fase 4 (§5.9-5.11): escritura idempotente de métricas y clasificación."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM hrv.metrics WHERE session_id = %s", (session_id,))
            cur.executemany(
                "INSERT INTO hrv.metrics (session_id, metric, value) VALUES (%s, %s, %s)",
                [(session_id, k, v) for k, v in sorted(result["metrics"].items())],
            )
            cur.execute(
                "INSERT INTO hrv.stress_results (session_id, analyzer, label, score, rules_met)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (session_id) DO UPDATE SET"
                " label = EXCLUDED.label, score = EXCLUDED.score,"
                " rules_met = EXCLUDED.rules_met, computed_at = NOW()",
                (session_id, pipeline.ANALYZER, result["label"], result["score"], result["rules_met"]),
            )
            cur.execute("DELETE FROM hrv.failures WHERE session_id = %s", (session_id,))

    def save_failure(self, session_id, error, permanent):
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hrv.failures (session_id, error, permanent) VALUES (%s, %s, %s)"
                " ON CONFLICT (session_id) DO UPDATE SET"
                " attempts = hrv.failures.attempts + 1, error = EXCLUDED.error,"
                " permanent = EXCLUDED.permanent, last_attempt = NOW()",
                (session_id, error, permanent),
            )


def analyze_session(db, client, session_id):
    df = db.load_samples(session_id)
    try:
        result = pipeline.analyze(df)
    except pipeline.AnalysisError as e:
        log.warning("sesión %d no analizable: %s", session_id, e)
        db.save_failure(session_id, str(e), permanent=True)
        return
    except Exception as e:
        # Un fallo de procesamiento no toca la sesión; se reintenta en el
        # siguiente sondeo hasta MAX_ATTEMPTS.
        log.exception("fallo procesando la sesión %d", session_id)
        db.save_failure(session_id, f"{type(e).__name__}: {e}", permanent=False)
        return

    db.save_result(session_id, result)
    m = result["metrics"]
    log.info("sesión %d: %s score=%s (sdnn=%.1f rmssd=%.1f pnn50=%.1f lf/hf=%.2f sqi=%.2f)",
             session_id, result["label"], result["score"],
             m.get("sdnn", float("nan")), m.get("rmssd", float("nan")), m.get("pnn50", float("nan")),
             m.get("lf_hf_ratio", float("nan")), m.get("sqi", float("nan")))
    client.publish(f"analysis/{session_id}/complete", json.dumps({
        "session_id": session_id, "analyzer": pipeline.ANALYZER,
        "label": result["label"], "score": result["score"],
    }), qos=QOS)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.error("conexión MQTT rechazada: %s", reason_code)
        return
    log.info("conectado a %s:%d", MQTT_HOST, MQTT_PORT)
    client.subscribe("analysis/+/request", QOS)


def on_message(client, requests, msg):
    parts = msg.topic.split("/")
    if len(parts) == 3 and parts[2] == "request" and parts[1].isdigit():
        requests.put(int(parts[1]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = Database()
    requests = queue.Queue()

    # Sesión persistente: los avisos que lleguen con el Analyzer parado se
    # entregan al reconectar. Aun así, el sondeo es la red de seguridad.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                         clean_session=False, userdata=requests)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    schema_ready = False
    next_poll = 0.0
    # Un solo hilo procesa: los avisos MQTT solo encolan identificadores.
    while True:
        try:
            if not schema_ready:
                db.ensure_schema()
                schema_ready = True
                log.info("esquema hrv listo")
            try:
                session_id = requests.get(timeout=max(0.0, next_poll - time.monotonic()))
                pending = db.pending_sessions(session_id)
            except queue.Empty:
                pending = db.pending_sessions()
                next_poll = time.monotonic() + POLL_SECONDS
                if pending:
                    log.info("sondeo: %d sesiones pendientes", len(pending))
            for sid in pending:
                analyze_session(db, client, sid)
        except psycopg2.Error:
            log.exception("fallo de base de datos; reintento en 5 s")
            db.reset()
            time.sleep(5)


if __name__ == "__main__":
    main()
