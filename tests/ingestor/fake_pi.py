"""Simula la Raspberry Pi: publica una sesión sintética y espera el ACK.

Genera 5 minutos de señal PPG a 50 Hz (15 000 muestras), la escribe como
JSONL, la comprime con gzip, calcula el SHA-256 del archivo y publica en
ppg/{device_key}/session, igual que describe docs/flujo.md §3.6-3.8.

Imprime el ACK recibido como JSON. Sale con 0 si llegó un ACK y su status
es el esperado (--expect), con 1 si no, y con 2 si no llegó ACK.

Uso (desde la raíz del repo, con los servicios arriba):
    docker compose run --rm --no-deps -v ./tests:/tests ingestor \
        python /tests/ingestor/fake_pi.py --device test-pi
"""

import argparse
import base64
import gzip
import hashlib
import json
import math
import sys
import threading
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt


def build_payload(samples, rate_hz, corrupt_hash):
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=6)
    step = timedelta(seconds=1 / rate_hz)
    lines = []
    for n in range(samples):
        # ~72 lpm modulado por una respiración de ~15 rpm
        beat = math.sin(2 * math.pi * 1.2 * n / rate_hz)
        resp = math.sin(2 * math.pi * 0.25 * n / rate_hz)
        lines.append(json.dumps({
            "time": (start + n * step).isoformat(),
            "ir": int(100000 + 1500 * beat + 300 * resp),
            "red": int(80000 + 1000 * beat + 200 * resp),
        }))
    blob = gzip.compress(("\n".join(lines) + "\n").encode())
    file_hash = hashlib.sha256(blob).hexdigest()
    if corrupt_hash:
        file_hash = "0" * 64
    return {
        "started_at": start.isoformat(),
        "finished_at": (start + samples * step).isoformat(),
        "sample_count": samples,
        "file_hash": file_hash,
        "data": base64.b64encode(blob).decode(),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="mosquitto")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--device", default="test-pi")
    p.add_argument("--samples", type=int, default=15000)
    p.add_argument("--rate", type=float, default=50.0)
    p.add_argument("--corrupt-hash", action="store_true",
                   help="envía un file_hash que no coincide (debe producir ACK de error)")
    p.add_argument("--expect", default="received", choices=["received", "error"])
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args()

    ack = {}
    got_ack = threading.Event()
    subscribed = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(f"ppg/{args.device}/ack", qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        subscribed.set()

    def on_message(client, userdata, msg):
        ack.update(json.loads(msg.payload))
        got_ack.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect(args.host, args.port)
    client.loop_start()
    try:
        if not subscribed.wait(args.timeout):
            print("no se pudo suscribir al topic de ACK", file=sys.stderr)
            return 2
        client.publish(f"ppg/{args.device}/status", "online", qos=1)
        payload = build_payload(args.samples, args.rate, args.corrupt_hash)
        client.publish(f"ppg/{args.device}/session", json.dumps(payload), qos=1).wait_for_publish()
        if not got_ack.wait(args.timeout):
            print(f"sin ACK tras {args.timeout:.0f} s", file=sys.stderr)
            return 2
    finally:
        client.loop_stop()
        client.disconnect()

    print(json.dumps(ack))
    return 0 if ack.get("status") == args.expect else 1


if __name__ == "__main__":
    sys.exit(main())
