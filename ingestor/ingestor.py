"""PulsePPG Ingestor.

Se suscribe a ppg/+/session, valida el payload, persiste la sesión y sus
muestras en una sola transacción, publica el ACK con el session_code que
generó la base de datos y avisa en analysis/{session_id}/request de que hay
una sesión disponible. También actualiza devices.last_seen con los mensajes
de ppg/+/status. No analiza, no genera códigos y no mantiene estados de
análisis (docs/flujo.md §4).

Payload esperado en ppg/{device_key}/session (JSON):

    {
      "started_at":   "2026-01-01T10:00:00+00:00",
      "finished_at":  "2026-01-01T10:05:00+00:00",
      "sample_count": 15000,
      "file_hash":    "<sha256 hex del archivo .jsonl.gz>",
      "data":         "<archivo .jsonl.gz en base64>"
    }

Cada línea del JSONL es una muestra: {"time": "<ISO 8601>", "ir": int, "red": int}.

Configuración por variables de entorno: MQTT_HOST, MQTT_PORT y las variables
estándar de libpq (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD).
"""

import base64
import binascii
import gzip
import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime

import paho.mqtt.client as mqtt
import psycopg2
from psycopg2 import errors

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
CLIENT_ID = "pulseppg-ingestor"
QOS = 1
CODE_RETRIES = 5

HASH_RE = re.compile(r"^[0-9a-f]{64}$")

log = logging.getLogger("ingestor")


class InvalidPayload(Exception):
    pass


class UnknownDevice(Exception):
    pass


# ------------------------------------------------------------
# Validación
# ------------------------------------------------------------

def _parse_time(value, field):
    if not isinstance(value, str):
        raise InvalidPayload(f"{field} debe ser una fecha ISO 8601")
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidPayload(f"{field} no es una fecha ISO 8601: {value!r}")
    if t.tzinfo is None:
        raise InvalidPayload(f"{field} debe incluir zona horaria")
    return t


def parse_session(raw):
    """Valida el payload y devuelve (metadatos, muestras). No toca la BD."""
    try:
        msg = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidPayload("el payload no es JSON")
    if not isinstance(msg, dict):
        raise InvalidPayload("el payload debe ser un objeto JSON")

    missing = [k for k in ("started_at", "finished_at", "sample_count", "file_hash", "data")
               if k not in msg]
    if missing:
        raise InvalidPayload(f"faltan campos: {', '.join(missing)}")

    started_at = _parse_time(msg["started_at"], "started_at")
    finished_at = _parse_time(msg["finished_at"], "finished_at")
    if finished_at <= started_at:
        raise InvalidPayload("finished_at debe ser posterior a started_at")

    sample_count = msg["sample_count"]
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 0:
        raise InvalidPayload("sample_count debe ser un entero >= 0")

    file_hash = msg["file_hash"]
    if not isinstance(file_hash, str) or not HASH_RE.match(file_hash):
        raise InvalidPayload("file_hash debe ser SHA-256 en hexadecimal en minúsculas")

    try:
        blob = base64.b64decode(msg["data"], validate=True)
    except (TypeError, binascii.Error):
        raise InvalidPayload("data no es base64 válido")
    if hashlib.sha256(blob).hexdigest() != file_hash:
        raise InvalidPayload("file_hash no coincide con el archivo recibido")

    try:
        text = gzip.decompress(blob).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError):
        raise InvalidPayload("data no es un archivo gzip de texto UTF-8")

    samples = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            s = json.loads(line)
            t = _parse_time(s["time"], "time")
            ir, red = s["ir"], s["red"]
        except (json.JSONDecodeError, KeyError, TypeError, InvalidPayload):
            raise InvalidPayload(f"muestra inválida en la línea {n}")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (ir, red)):
            raise InvalidPayload(f"ir/red deben ser enteros en la línea {n}")
        samples.append((t, ir, red))

    if len(samples) != sample_count:
        raise InvalidPayload(f"sample_count={sample_count} pero el archivo trae {len(samples)} muestras")

    meta = {
        "started_at": started_at,
        "finished_at": finished_at,
        "sample_count": sample_count,
        "file_hash": file_hash,
    }
    return meta, samples


# ------------------------------------------------------------
# Persistencia
# ------------------------------------------------------------

class Database:
    def __init__(self):
        self.conn = None

    def _connection(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect("")
        return self.conn

    def _reset(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except psycopg2.Error:
                pass
        self.conn = None

    def store_session(self, device_key, meta, samples):
        """Persiste sesión y muestras en una transacción. Devuelve (id, session_code)."""
        for attempt in range(1, CODE_RETRIES + 1):
            conn = self._connection()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SELECT id FROM devices WHERE device_key = %s", (device_key,))
                    row = cur.fetchone()
                    if row is None:
                        raise UnknownDevice(device_key)

                    cur.execute(
                        "INSERT INTO sessions"
                        " (device_id, started_at, finished_at, sample_count, file_hash)"
                        " VALUES (%s, %s, %s, %s, %s)"
                        " RETURNING id, session_code",
                        (row[0], meta["started_at"], meta["finished_at"],
                         meta["sample_count"], meta["file_hash"]),
                    )
                    session_id, session_code = cur.fetchone()

                    buf = io.StringIO()
                    for t, ir, red in samples:
                        buf.write(f"{t.isoformat()}\t{session_id}\t{ir}\t{red}\n")
                    buf.seek(0)
                    cur.copy_expert("COPY raw_ppg (time, session_id, ir, red) FROM STDIN", buf)
                return session_id, session_code
            except errors.UniqueViolation as e:
                # La unicidad del código la arbitra PostgreSQL: si colisiona,
                # se reintenta la transacción completa (ya revertida).
                if e.diag.constraint_name == "sessions_session_code_key" and attempt < CODE_RETRIES:
                    log.warning("colisión de session_code, reintento %d", attempt)
                    continue
                raise
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._reset()
                raise

    def touch_device(self, device_key):
        conn = self._connection()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("UPDATE devices SET last_seen = NOW() WHERE device_key = %s",
                            (device_key,))
                return cur.rowcount
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            self._reset()
            raise


# ------------------------------------------------------------
# MQTT
# ------------------------------------------------------------

def publish_json(client, topic, body):
    client.publish(topic, json.dumps(body), qos=QOS)


def handle_session(client, db, device_key, payload):
    ack_topic = f"ppg/{device_key}/ack"
    try:
        meta, samples = parse_session(payload)
    except InvalidPayload as e:
        log.warning("sesión descartada de %s: %s", device_key, e)
        publish_json(client, ack_topic, {"status": "error", "error": "invalid_payload"})
        return

    try:
        session_id, session_code = db.store_session(device_key, meta, samples)
    except UnknownDevice:
        log.warning("sesión descartada: dispositivo no registrado %r", device_key)
        publish_json(client, ack_topic, {"status": "error", "error": "unknown_device"})
        return
    except psycopg2.Error:
        log.exception("fallo persistiendo la sesión de %s; transacción revertida", device_key)
        publish_json(client, ack_topic, {"status": "error", "error": "internal"})
        return

    log.info("sesión %d (%s) de %s: %d muestras", session_id, session_code, device_key, len(samples))
    publish_json(client, ack_topic,
                 {"session_id": session_id, "session_code": session_code, "status": "received"})
    # Solo un aviso: si falla, la sesión ya está persistida.
    publish_json(client, f"analysis/{session_id}/request", {"session_id": session_id})


def handle_status(db, device_key, payload):
    try:
        if db.touch_device(device_key) == 0:
            log.warning("status de un dispositivo no registrado: %r", device_key)
        else:
            log.info("status de %s: %s", device_key, payload.decode("utf-8", "replace"))
    except psycopg2.Error:
        log.exception("fallo actualizando last_seen de %s", device_key)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.error("conexión MQTT rechazada: %s", reason_code)
        return
    log.info("conectado a %s:%d", MQTT_HOST, MQTT_PORT)
    client.subscribe([("ppg/+/session", QOS), ("ppg/+/status", QOS)])


def on_message(client, db, msg):
    parts = msg.topic.split("/")
    if len(parts) != 3 or parts[0] != "ppg" or not parts[1]:
        return
    device_key, kind = parts[1], parts[2]
    if kind == "session":
        handle_session(client, db, device_key, msg.payload)
    elif kind == "status":
        handle_status(db, device_key, msg.payload)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = Database()
    # Sesión persistente (clean_session=False): si el Ingestor se reinicia,
    # Mosquitto guarda los mensajes QoS 1 que lleguen mientras tanto.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                         clean_session=False, userdata=db)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
