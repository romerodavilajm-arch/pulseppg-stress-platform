"""PulsePPG: programa del nodo Raspberry Pi 5 + MAX30102.

Hace lo que describe docs/flujo.md §3 y nada más: detecta el dedo, captura
5 minutos de PPG a 50 Hz en un archivo JSONL, lo comprime con gzip, calcula el
SHA-256 del archivo y lo publica en ppg/{device_key}/session. Espera el ACK en
ppg/{device_key}/ack y muestra el session_code que generó la base de datos.
No genera códigos ni conoce la base de datos.

Contrato MQTT (el mismo que cumple tests/ingestor/fake_pi.py):
  ppg/{device_key}/status   "online" al conectar, "offline" como testamento (LWT)
  ppg/{device_key}/session  {"started_at", "finished_at", "sample_count",
                             "file_hash", "data"}; cada línea del JSONL es
                             {"time": ISO 8601, "ir": int, "red": int}
  ppg/{device_key}/ack      {"session_id", "session_code", "status": "received"}
                            o {"status": "error", "error": ...}

Cada captura terminada se guarda primero en SPOOL_DIR/pending y solo se borra
cuando llega su ACK, así que un corte de red o de luz no la pierde. Sin ACK
tras SEND_ATTEMPTS intentos se reintenta cada RETRY_SECONDS mientras el nodo
está en reposo. El reenvío lleva el mismo archivo y el mismo hash, y el
Ingestor responde con la sesión original sin duplicarla.

Configuración por variables de entorno (ver pulseppg.env.example).

Uso:
    python pulseppg_node.py              # en la Pi, con el sensor
    python pulseppg_node.py --simulate   # sin sensor: una captura sintética y sale
"""

import argparse
import base64
import collections
import gzip
import hashlib
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

SAMPLE_RATE_HZ = 50
WINDOW = SAMPLE_RATE_HZ // 2          # calidad cada 500 ms (§3.3)
FINGER_WINDOWS = 4                    # 2 s seguidos dentro de umbral = dedo (§3.1)
LOST_WINDOWS = 3                      # degradada más de 1 s = señal perdida (§3.4)
PI_HISTORY = 2 * SAMPLE_RATE_HZ       # amplitud del pulso sobre los últimos 2 s

log = logging.getLogger("pulseppg")


# ------------------------------------------------------------
# Configuración
# ------------------------------------------------------------

def env(name, default=None, cast=str):
    value = os.environ.get(name, "").strip()
    if not value:
        if default is None:
            sys.exit(f"falta la variable de entorno {name} (ver pulseppg.env.example)")
        return default
    return cast(value)


def load_config():
    return {
        "device_key": env("DEVICE_KEY"),
        "mqtt_host": env("MQTT_HOST"),
        "mqtt_port": env("MQTT_PORT", 1883, int),
        "i2c_bus": env("I2C_BUS", 1, int),
        "sensor_address": env("SENSOR_ADDRESS", 0x57, lambda v: int(v, 0)),
        "led_red": env("LED_RED_PA", 0x24, lambda v: int(v, 0)),
        "led_ir": env("LED_IR_PA", 0x24, lambda v: int(v, 0)),
        "lcd_address": env("LCD_ADDRESS", 0, lambda v: int(v, 0)),
        "buzzer_pin": env("BUZZER_PIN", 0, int),
        # systemd define STATE_DIRECTORY con StateDirectory=pulseppg.
        "spool_dir": Path(env("SPOOL_DIR", os.environ.get("STATE_DIRECTORY", "/var/lib/pulseppg"))),
        "capture_seconds": env("CAPTURE_SECONDS", 300, int),
        "ack_timeout": env("ACK_TIMEOUT", 10.0, float),
        "send_attempts": env("SEND_ATTEMPTS", 3, int),
        "retry_seconds": env("RETRY_SECONDS", 300, float),
        "code_seconds": env("CODE_SECONDS", 20, float),
        "remove_timeout": env("REMOVE_TIMEOUT", 30, float),
        # Umbrales de detección del dedo: provisionales, se calibran con
        # check_sensor.py (ver setup.md).
        "ir_min": env("FINGER_IR_MIN", 50000, int),
        "ratio_min": env("FINGER_RATIO_MIN", 0.5, float),
        "ratio_max": env("FINGER_RATIO_MAX", 3.0, float),
        "pi_min": env("FINGER_PI_MIN", 0.0005, float),
        "pi_max": env("FINGER_PI_MAX", 0.05, float),
    }


# ------------------------------------------------------------
# Hardware: display, buzzer y sensor simulado
# ------------------------------------------------------------

class Display:
    """LCD 16x2 con adaptador I2C PCF8574, o solo el log si no hay LCD."""

    def __init__(self, bus, address):
        self.lcd = None
        if address:
            from RPLCD.i2c import CharLCD
            self.lcd = CharLCD("PCF8574", address, port=bus, cols=16, rows=2,
                               charmap="A00", auto_linebreaks=False)
            self.lcd.clear()

    def show(self, line1, line2=""):
        log.info("display: %s | %s", line1, line2)
        if self.lcd:
            # El HD44780 no tiene acentos ni ñ: los textos van en ASCII.
            self.lcd.cursor_pos = (0, 0)
            self.lcd.write_string(line1[:16].ljust(16))
            self.lcd.cursor_pos = (1, 0)
            self.lcd.write_string(line2[:16].ljust(16))

    def close(self):
        if self.lcd:
            self.lcd.clear()
            self.lcd.close(clear=True)


class Buzzer:
    """Buzzer activo en un pin GPIO (numeración BCM), o nada si no hay pin."""

    def __init__(self, pin):
        self.pin = pin
        self.gpio = None
        if pin:
            # En la Pi 5, RPi.GPIO lo provee rpi-lgpio (ver setup.md).
            import RPi.GPIO as GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
            self.gpio = GPIO

    def beep(self, seconds=0.15, times=1):
        if not self.gpio:
            return
        for i in range(times):
            self.gpio.output(self.pin, self.gpio.HIGH)
            time.sleep(seconds)
            self.gpio.output(self.pin, self.gpio.LOW)
            if i < times - 1:
                time.sleep(seconds)

    def close(self):
        if self.gpio:
            self.gpio.cleanup(self.pin)


class FakeSensor:
    """Sustituye al MAX30102 con --simulate: sin dedo 2 s, dedo durante la
    captura y unos segundos más, y luego sin dedo. Mismo interfaz que MAX30102."""

    def __init__(self, capture_seconds, seed=1):
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()
        self.emitted = 0
        self.finger_from = 2.0
        self.finger_until = self.finger_from + capture_seconds + 8.0
        self.overflows = 0
        self.beat_at, self.next_beat = 0.0, 0.85

    def clear_fifo(self):
        self.emitted = int((time.monotonic() - self.t0) * SAMPLE_RATE_HZ)

    def _sample(self, t):
        if not (self.finger_from <= t < self.finger_until):
            return (int(1000 + self.rng.gauss(0, 50)), int(800 + self.rng.gauss(0, 50)))
        while t >= self.next_beat:
            self.beat_at = self.next_beat
            self.next_beat += 0.85 + 0.04 * math.sin(2 * math.pi * 0.25 * t) + self.rng.gauss(0, 0.02)
        phase = (t - self.beat_at) / (self.next_beat - self.beat_at)
        pulse = math.exp(-((phase - 0.2) / 0.07) ** 2) + 0.35 * math.exp(-((phase - 0.48) / 0.09) ** 2)
        return (int(120000 + 1500 * pulse), int(90000 + 1000 * pulse))

    def read(self):
        due = int((time.monotonic() - self.t0) * SAMPLE_RATE_HZ)
        out = [self._sample(n / SAMPLE_RATE_HZ) for n in range(self.emitted, due)]
        self.emitted = due
        return out

    def close(self):
        pass


# ------------------------------------------------------------
# Calidad local (§3.1, §3.3, §3.4)
# ------------------------------------------------------------

class QualityGate:
    """Evalúa cada 500 ms si hay un dedo con señal útil.

    - Nivel continuo (DC) del canal IR por encima de FINGER_IR_MIN.
    - Ratio IR/rojo dentro de [FINGER_RATIO_MIN, FINGER_RATIO_MAX].
    - Amplitud pulsátil (pico a pico / DC del IR en los últimos 2 s) dentro de
      [FINGER_PI_MIN, FINGER_PI_MAX]: sin pulso no hay dedo bien apoyado y un
      valor muy alto indica movimiento. Es un SQI aproximado de ventana corta;
      el control de calidad real lo hace el Analyzer sobre los 5 minutos.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.window = []
        self.history = collections.deque(maxlen=PI_HISTORY)

    def push(self, ir, red):
        """Añade una muestra. Cada WINDOW muestras devuelve un dict con el
        veredicto de la ventana; el resto de las veces devuelve None."""
        self.window.append((ir, red))
        self.history.append(ir)
        if len(self.window) < WINDOW:
            return None
        ir_dc = sum(s[0] for s in self.window) / WINDOW
        red_dc = sum(s[1] for s in self.window) / WINDOW
        self.window = []
        c = self.cfg
        present = ir_dc >= c["ir_min"]
        ratio = ir_dc / red_dc if red_dc else 0.0
        pi = (max(self.history) - min(self.history)) / ir_dc if ir_dc else 0.0
        ok = (present and c["ratio_min"] <= ratio <= c["ratio_max"]
              and len(self.history) == PI_HISTORY and c["pi_min"] <= pi <= c["pi_max"])
        return {"present": present, "ok": ok, "ir_dc": ir_dc, "ratio": ratio, "pi": pi}


# ------------------------------------------------------------
# MQTT
# ------------------------------------------------------------

class Sender:
    def __init__(self, cfg):
        self.device = cfg["device_key"]
        self.timeout = cfg["ack_timeout"]
        self.ack = {}
        self.got_ack = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"pulseppg-node-{self.device}")
        self.client.will_set(f"ppg/{self.device}/status", "offline", qos=1)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(cfg["mqtt_host"], cfg["mqtt_port"])
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("conexión MQTT rechazada: %s", reason_code)
            return
        log.info("conectado al broker MQTT")
        client.subscribe(f"ppg/{self.device}/ack", qos=1)
        client.publish(f"ppg/{self.device}/status", "online", qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            self.ack = json.loads(msg.payload)
        except ValueError:
            log.warning("ACK ilegible: %r", msg.payload[:200])
            return
        self.got_ack.set()

    def send(self, payload_text):
        """Publica una sesión y espera su ACK. Devuelve el ACK o None."""
        if not self.client.is_connected():
            log.warning("sin conexión con el broker")
            time.sleep(min(self.timeout, 5))
            return None
        self.got_ack.clear()
        info = self.client.publish(f"ppg/{self.device}/session", payload_text, qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish falló: %s", mqtt.error_string(info.rc))
            return None
        if not self.got_ack.wait(self.timeout):
            log.warning("sin ACK tras %.0f s", self.timeout)
            return None
        return dict(self.ack)

    def close(self):
        if self.client.is_connected():
            self.client.publish(f"ppg/{self.device}/status", "offline", qos=1).wait_for_publish(2)
        self.client.disconnect()
        self.client.loop_stop()


# ------------------------------------------------------------
# Nodo
# ------------------------------------------------------------

IDLE, CAPTURING, WAIT_REMOVE = "idle", "capturing", "wait_remove"


class Node:
    def __init__(self, cfg, sensor, display, buzzer, sender, once=False):
        self.cfg, self.sensor, self.display, self.buzzer, self.sender = cfg, sensor, display, buzzer, sender
        self.once = once
        self.gate = QualityGate(cfg)
        self.spool = cfg["spool_dir"]
        self.pending = self.spool / "pending"
        self.rejected = self.spool / "rejected"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.rejected.mkdir(parents=True, exist_ok=True)
        self.capture_path = self.spool / "capture.jsonl"
        self.total = cfg["capture_seconds"] * SAMPLE_RATE_HZ
        self.next_retry = 0.0
        self.done = False

    # --- estados ---------------------------------------------------------

    def go_idle(self):
        self.state = IDLE
        self.streak = 0
        self.gate.reset()
        self.sensor.clear_fifo()
        self.display.show("PulsePPG listo", "Coloca el dedo")

    def start_capture(self):
        self.state = CAPTURING
        self.bad = 0
        self.n = 0
        # Se conserva el historial de 2 s de la detección: sin él, las
        # primeras ventanas de la captura no tendrían amplitud de pulso.
        self.gate.window = []
        self.sensor.clear_fifo()
        self.sensor.overflows = 0
        self.started_at = datetime.now(timezone.utc)
        self.file = open(self.capture_path, "w", encoding="utf-8")
        self.last_shown = None
        self.buzzer.beep()
        self.show_countdown()

    def show_countdown(self):
        left = math.ceil((self.total - self.n) / SAMPLE_RATE_HZ)
        if left != self.last_shown:
            self.last_shown = left
            self.display.show(f"Capturando {left // 60}:{left % 60:02d}", "No muevas el dedo")

    def abort_capture(self, why):
        log.warning("captura descartada: %s", why)
        self.file.close()
        self.capture_path.unlink(missing_ok=True)
        self.buzzer.beep(0.4)
        self.display.show("Senal perdida.", "Reintentando...")
        time.sleep(2)
        self.go_idle()

    # --- bucle -----------------------------------------------------------

    def run(self):
        self.go_idle()
        while not self.done:
            time.sleep(0.1)
            batch = self.sensor.read()
            if self.state == CAPTURING and self.sensor.overflows:
                # Se perdieron muestras: los tiempos ya no serían correctos.
                self.abort_capture("desbordamiento del FIFO del sensor")
                continue
            for ir, red in batch:
                if not self.on_sample(ir, red):
                    break
            if self.state == WAIT_REMOVE and time.monotonic() > self.remove_deadline:
                self.finish()
            if self.state == IDLE and self.streak == 0:
                self.retry_pending()

    def on_sample(self, ir, red):
        """Procesa una muestra. Devuelve False si el resto del lote ya no vale
        (cambió el estado y se vació el FIFO)."""
        if self.state == CAPTURING:
            t = self.started_at + timedelta(seconds=self.n / SAMPLE_RATE_HZ)
            self.file.write(json.dumps({"time": t.isoformat(), "ir": ir, "red": red}) + "\n")
            self.n += 1
        verdict = self.gate.push(ir, red)

        if self.state == CAPTURING:
            if self.n >= self.total:
                self.file.close()
                self.state = WAIT_REMOVE
                self.remove_deadline = time.monotonic() + self.cfg["remove_timeout"]
                self.buzzer.beep(times=2)
                self.display.show("Prueba completada", "Retira el dedo")
                return False
            if verdict:
                self.bad = 0 if verdict["ok"] else self.bad + 1
                if self.bad >= LOST_WINDOWS:
                    self.abort_capture(f"calidad baja ({verdict})")
                    return False
            self.show_countdown()
            return True

        if verdict is None:
            return True
        if self.state == IDLE:
            self.streak = self.streak + 1 if verdict["ok"] else 0
            if self.streak >= FINGER_WINDOWS:
                if not clock_ok():
                    self.display.show("Sin hora (NTP)", "Revisa la red")
                    self.streak = 0
                    return True
                self.start_capture()
                return False
        elif self.state == WAIT_REMOVE and not verdict["present"]:
            self.finish()
            return False
        return True

    # --- cierre y envío (§3.6-3.8) ---------------------------------------

    def finish(self):
        raw = self.capture_path.read_bytes()
        blob = gzip.compress(raw, mtime=0)
        file_hash = hashlib.sha256(blob).hexdigest()
        finished_at = self.started_at + timedelta(seconds=self.total / SAMPLE_RATE_HZ)
        payload = json.dumps({
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "sample_count": self.total,
            "file_hash": file_hash,
            "data": base64.b64encode(blob).decode(),
        })
        name = f"{self.started_at:%Y%m%dT%H%M%SZ}_{file_hash[:12]}.json"
        tmp = self.pending / (name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.pending / name)
        self.capture_path.unlink(missing_ok=True)
        log.info("captura guardada: %s (%d muestras, %d bytes gzip)", name, self.total, len(blob))

        self.display.show("Enviando...")
        acks = self.flush_pending(self.cfg["send_attempts"])
        ack = acks.get(name)
        if ack is None:
            self.display.show("Sin conexion.", "Se reintentara")
        else:
            self.show_ack(ack)
        time.sleep(self.cfg["code_seconds"])
        if self.once:
            self.done = True
            return
        self.go_idle()

    def show_ack(self, ack):
        if ack.get("status") == "received":
            self.buzzer.beep()
            self.display.show(f"CODIGO: {ack['session_code']}", "Consulta en web")
        elif ack.get("error") == "unknown_device":
            self.display.show("Disp. no regist.", "Avisa al admin")
        elif ack.get("error") == "invalid_payload":
            self.display.show("Captura invalida", "Repite la prueba")
        else:
            self.display.show("Error servidor", "Se reintentara")

    def flush_pending(self, attempts):
        """Envía las capturas pendientes, de la más antigua a la más nueva.

        Se detiene en la primera que no recibe ACK de éxito, para que un ACK
        que llegue tarde corresponda siempre a la misma captura. Devuelve
        {nombre: último ACK recibido}.
        """
        acks = {}
        for path in sorted(self.pending.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            ack = None
            for attempt in range(1, attempts + 1):
                log.info("enviando %s (intento %d/%d)", path.name, attempt, attempts)
                ack = self.sender.send(text)
                if ack is not None:
                    break
            if ack is None:
                self.next_retry = time.monotonic() + self.cfg["retry_seconds"]
                return acks
            acks[path.name] = ack
            if ack.get("status") == "received":
                log.info("ACK de %s: sesión %s, código %s", path.name,
                         ack.get("session_id"), ack.get("session_code"))
                path.unlink()
            elif ack.get("error") == "invalid_payload":
                # Reenviarla no la arreglaría: se aparta para revisarla.
                log.error("el servidor rechazó %s como inválida; se mueve a rejected/", path.name)
                path.replace(self.rejected / path.name)
            else:
                log.error("ACK de error para %s: %s; se reintentará", path.name, ack)
                self.next_retry = time.monotonic() + self.cfg["retry_seconds"]
                return acks
        return acks

    def retry_pending(self):
        if time.monotonic() < self.next_retry or not any(self.pending.glob("*.json")):
            return
        acks = self.flush_pending(1)
        received = [a for a in acks.values() if a.get("status") == "received"]
        if received:
            # §3.8: cuando por fin llega el ACK, se muestra el código.
            self.show_ack(received[-1])
            time.sleep(self.cfg["code_seconds"])
        self.go_idle()


def clock_ok():
    """Sin RTC con batería ni NTP, la Pi arranca con una fecha antigua."""
    return datetime.now(timezone.utc).year >= 2025


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--simulate", action="store_true",
                   help="sin sensor, LCD ni buzzer: genera una captura sintética, la envía y sale")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()

    # systemd detiene el servicio con SIGTERM: salir limpio publica "offline".
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    display = buzzer = sensor = sender = None
    try:
        if args.simulate:
            sensor = FakeSensor(cfg["capture_seconds"])
            display, buzzer = Display(0, 0), Buzzer(0)
        else:
            from max30102 import MAX30102
            sensor = MAX30102(cfg["i2c_bus"], cfg["sensor_address"], cfg["led_red"], cfg["led_ir"])
            display = Display(cfg["i2c_bus"], cfg["lcd_address"])
            buzzer = Buzzer(cfg["buzzer_pin"])
        sender = Sender(cfg)
        Node(cfg, sensor, display, buzzer, sender, once=args.simulate).run()
    finally:
        for part in (sender, sensor, display, buzzer):
            if part is not None:
                try:
                    part.close()
                except Exception:
                    log.exception("fallo al cerrar %s", type(part).__name__)


if __name__ == "__main__":
    main()
