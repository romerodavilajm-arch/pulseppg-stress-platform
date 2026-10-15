"""Simula la Raspberry Pi: publica una sesión sintética y espera el ACK.

Genera 5 minutos de señal PPG a 50 Hz (15 000 muestras), la escribe como
JSONL, la comprime con gzip, calcula el SHA-256 del archivo y publica en
ppg/{device_key}/session, igual que describe docs/flujo.md §3.6-3.8.

Con --profile se elige la señal: "sine" (por defecto, senoide de 72 lpm),
"relaxed" y "stressed" (pulsos con intervalos RR de variabilidad alta o baja,
para probar el Analyzer) o "noisy" (ruido que tapa el pulso, debe salir
low_quality). Con --start la fecha de inicio es fija, de modo que dos envíos
iguales producen el mismo archivo y el mismo hash, como un reenvío de la Pi.

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
import random
import sys
import threading
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt


# Intervalos RR (s): media, amplitud de la oscilación LF (0.1 Hz), amplitud de
# la arritmia respiratoria HF (0.25 Hz) y desviación del ruido latido a latido.
RR_PROFILES = {
    "relaxed": (0.90, 0.020, 0.050, 0.020),   # ~67 lpm, HRV alta, HF dominante
    "stressed": (0.65, 0.012, 0.002, 0.002),  # ~92 lpm, HRV baja, LF dominante
    "noisy": (0.85, 0.010, 0.010, 0.010),
}


def pulse_shape(phase):
    """Forma de un latido PPG en [0, 1): pico sistólico y onda dicrótica."""
    return (math.exp(-((phase - 0.20) / 0.07) ** 2)
            + 0.35 * math.exp(-((phase - 0.48) / 0.09) ** 2))


def beat_signal(samples, rate_hz, profile, rng):
    """Devuelve una lista de valores de pulso (sin componente continua)."""
    if profile == "sine":
        # ~72 lpm modulado por una respiración de ~15 rpm
        return [math.sin(2 * math.pi * 1.2 * n / rate_hz)
                + 0.2 * math.sin(2 * math.pi * 0.25 * n / rate_hz) for n in range(samples)]

    mean, lf, hf, jitter = RR_PROFILES[profile]
    duration = samples / rate_hz
    beats = [0.0]
    while beats[-1] < duration + 2:
        t = beats[-1]
        beats.append(t + mean + lf * math.sin(2 * math.pi * 0.1 * t)
                     + hf * math.sin(2 * math.pi * 0.25 * t) + rng.gauss(0, jitter))
    out, b = [], 0
    for n in range(samples):
        t = n / rate_hz
        while beats[b + 1] <= t:
            b += 1
        phase = (t - beats[b]) / (beats[b + 1] - beats[b])
        out.append(pulse_shape(phase) + 0.15 * math.sin(2 * math.pi * 0.25 * t))
    if profile == "noisy":
        # Movimiento del dedo: ruido de banda ancha varias veces mayor que el pulso.
        out = [v + rng.gauss(0, 2.0) for v in out]
    return out


def build_payload(samples, rate_hz, corrupt_hash, profile="sine", start=None, seed=1):
    if start is None:
        start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=6)
    step = timedelta(seconds=1 / rate_hz)
    rng = random.Random(seed)
    lines = []
    for n, beat in enumerate(beat_signal(samples, rate_hz, profile, rng)):
        lines.append(json.dumps({
            "time": (start + n * step).isoformat(),
            "ir": int(100000 + 1500 * beat),
            "red": int(80000 + 1000 * beat),
        }))
    # mtime=0: el mismo contenido da el mismo archivo (y hash), como un reenvío.
    blob = gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
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
    p.add_argument("--profile", default="sine", choices=["sine", *RR_PROFILES],
                   help="forma de la señal sintética")
    p.add_argument("--start", type=datetime.fromisoformat,
                   help="fecha de inicio fija (ISO 8601 con zona); por defecto, hace 6 minutos")
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
        payload = build_payload(args.samples, args.rate, args.corrupt_hash,
                                args.profile, args.start)
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
