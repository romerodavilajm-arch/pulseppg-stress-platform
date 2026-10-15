# Raspberry Pi 5 + MAX30102: puesta en marcha del nodo

Guía para dejar una Raspberry Pi 5 con Raspberry Pi OS Lite lista como nodo de captura de PulsePPG. El nodo hace lo que describe [`docs/flujo.md`](../docs/flujo.md) §3: detecta el dedo, captura 5 minutos de PPG a 50 Hz, comprime el archivo JSONL con gzip, calcula su SHA-256, lo publica por MQTT y muestra en el LCD el código de sesión que llega en el ACK. No conoce la base de datos ni genera códigos.

Es el mismo contrato que ya cumple el simulador [`tests/ingestor/fake_pi.py`](../tests/ingestor/fake_pi.py), así que el servidor (`compose.yaml`) no cambia.

---

## 1. Archivos de esta carpeta y dónde van en la Pi

| Archivo | Destino en la Pi | Qué es |
|---------|------------------|--------|
| `pulseppg_node.py` | `/opt/pulseppg/` | Programa del nodo: detección de dedo, captura, envío, ACK, reintentos, LCD y buzzer |
| `max30102.py` | `/opt/pulseppg/` | Lectura del MAX30102 por I2C (modo SpO2, 50 Hz, 18 bits) |
| `check_sensor.py` | `/opt/pulseppg/` | Prueba del sensor sin red: imprime IR y rojo cada medio segundo |
| `requirements.txt` | `/opt/pulseppg/` | Dependencias de Python del venv |
| `pulseppg.env.example` | `/etc/pulseppg/pulseppg.env` | Configuración: `DEVICE_KEY`, IP del servidor, pines, umbrales |
| `pulseppg.service` | `/etc/systemd/system/` | Servicio systemd `pulseppg` (arranque automático) |

En la Pi también aparecen, creados por el servicio:

- `/opt/pulseppg/venv/`: entorno virtual de Python.
- `/var/lib/pulseppg/pending/`: capturas terminadas que aún no tienen ACK. Se borran al recibirlo.
- `/var/lib/pulseppg/rejected/`: capturas que el servidor rechazó como inválidas (no se reintentan).

---

## 2. Material y conexiones

- Raspberry Pi 5, fuente USB-C oficial de 27 W y microSD de 16 GB o más.
- Módulo MAX30102 (dirección I2C `0x57`).
- LCD 16x2 con adaptador I2C PCF8574 (dirección `0x27` o `0x3F`). Opcional.
- Buzzer activo. Opcional.

Los dos dispositivos I2C comparten el bus 1 de la Pi:

| Señal | Pin físico de la Pi 5 | MAX30102 | LCD (PCF8574) |
|-------|-----------------------|----------|---------------|
| 3.3 V | 1 | VIN | |
| 5 V | 2 | | VCC |
| SDA (GPIO 2) | 3 | SDA | SDA |
| SCL (GPIO 3) | 5 | SCL | SCL |
| GND | 6 o 9 | GND | GND |
| GPIO 18 | 12 | | (buzzer, ver abajo) |

Precauciones:

- **MAX30102 siempre a 3.3 V.** Los GPIO de la Pi no toleran 5 V.
- **LCD a 5 V.** La mayoría de adaptadores PCF8574 traen resistencias pull-up a VCC; con VCC a 5 V suben SDA/SCL a 5 V. Retira esas resistencias (las del MAX30102 ya sirven a 3.3 V) o usa un conversor de nivel. Con el LCD a 3.3 V funciona, pero el contraste suele quedar muy bajo.
- **Buzzer con transistor.** Un buzzer activo consume más de lo que da un GPIO. Usa un NPN (por ejemplo 2N2222 con 1 kΩ en la base) o un módulo de buzzer que ya lo incluya, con la señal en GPIO 18.
- Si el LCD no muestra texto pero responde en `i2cdetect`, ajusta el potenciómetro de contraste del adaptador.

---

## 3. Instalar Raspberry Pi OS Lite

1. En tu PC, abre **Raspberry Pi Imager** y elige: dispositivo *Raspberry Pi 5*, sistema *Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)*, y la microSD.
2. En la personalización del sistema (Imager la ofrece antes de escribir):
   - Nombre de equipo: por ejemplo `pulseppg-1`.
   - Usuario y contraseña propios.
   - Wi-Fi con su país (o deja la Pi por cable Ethernet).
   - Zona horaria `America/Mexico_City` y teclado.
   - Activa **SSH** (con contraseña o con tu clave pública).
3. Escribe la tarjeta, insértala en la Pi y enciéndela. El primer arranque tarda un par de minutos.
4. Desde tu PC:

   ```bash
   ssh usuario@pulseppg-1.local
   ```

   Si `.local` no resuelve, busca la IP de la Pi en tu router y usa `ssh usuario@IP`.

5. Actualiza el sistema y reinicia:

   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```

---

## 4. Configurar el sistema

### 4.1. Activar I2C

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

Tras reiniciar, con el sensor (y el LCD) conectados:

```bash
sudo apt install -y i2c-tools
ls /dev/i2c-1
i2cdetect -y 1
```

Deben aparecer `57` (MAX30102) y `27` o `3f` (LCD). Si no aparece nada, revisa cableado y alimentación antes de seguir.

### 4.2. Hora sincronizada

Los tiempos de las muestras salen del reloj de la Pi. La Pi 5 no guarda la hora apagada si no le pones la batería del RTC, así que depende de NTP:

```bash
timedatectl                                   # debe decir "System clock synchronized: yes"
sudo systemctl enable systemd-time-wait-sync  # el servicio espera a tener hora antes de arrancar
```

Si la hora no está sincronizada, el nodo no empieza capturas y muestra `Sin hora (NTP)`.

### 4.3. Paquetes del sistema

```bash
sudo apt install -y python3-venv python3-rpi-lgpio git
```

En la Pi 5 la biblioteca clásica `RPi.GPIO` no funciona (los GPIO dependen del chip RP1). `python3-rpi-lgpio` ofrece la misma API `RPi.GPIO` sobre `lgpio`, y es lo que usa el buzzer. Si `apt` avisa de que reemplaza `python3-rpi.gpio`, acepta.

### 4.4. Usuario del servicio

El servicio corre con un usuario sin login, que solo tiene acceso a I2C y GPIO:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --groups i2c,gpio pulseppg
```

---

## 5. Copiar el programa y crear el entorno de Python

Lleva esta carpeta a la Pi. Con `git`:

```bash
git clone https://github.com/romerodavilajm-arch/pulseppg-stress-platform.git ~/pulseppg-stress-platform
cd ~/pulseppg-stress-platform/raspberry-pi
```

O desde tu PC, si el repositorio no es accesible desde la Pi:

```bash
scp -r raspberry-pi usuario@pulseppg-1.local:~/
# y en la Pi: cd ~/raspberry-pi
```

Instala los archivos y el entorno virtual (desde la carpeta `raspberry-pi` en la Pi):

```bash
sudo mkdir -p /opt/pulseppg /etc/pulseppg
sudo cp pulseppg_node.py max30102.py check_sensor.py requirements.txt /opt/pulseppg/
sudo python3 -m venv --system-site-packages /opt/pulseppg/venv
sudo /opt/pulseppg/venv/bin/pip install -r /opt/pulseppg/requirements.txt
```

`--system-site-packages` deja ver dentro del venv el `RPi.GPIO` que instaló `python3-rpi-lgpio`. El resto (`paho-mqtt` 2.1, la misma versión que usa el Ingestor, `smbus2` y `RPLCD`) queda fijado en el venv.

Configuración:

```bash
sudo cp pulseppg.env.example /etc/pulseppg/pulseppg.env
sudo nano /etc/pulseppg/pulseppg.env
```

Como mínimo cambia `DEVICE_KEY` (la clave con la que darás de alta el dispositivo) y `MQTT_HOST` (la IP del equipo que corre `docker compose`). Si no tienes LCD o buzzer, deja vacíos `LCD_ADDRESS=` o `BUZZER_PIN=`.

---

## 6. Probar el sensor

Sin red ni servidor. Tu usuario ya pertenece al grupo `i2c`, así que no hace falta `sudo`:

```bash
/opt/pulseppg/venv/bin/python /opt/pulseppg/check_sensor.py --seconds 20
```

Imprime cada medio segundo cuántas muestras llegaron (unas 25), la media y la amplitud pico a pico de IR y rojo, y el ratio IR/rojo. Coloca y retira el dedo para ver la diferencia y ajusta en `pulseppg.env`:

- `FINGER_IR_MIN`: un valor entre el IR medio sin dedo y con dedo.
- `FINGER_RATIO_MIN` y `FINGER_RATIO_MAX`: un rango que incluya el ratio IR/rojo con el dedo apoyado.
- `FINGER_PI_MIN` y `FINGER_PI_MAX`: rango de amplitud pulsátil relativa (IR p-p / IR medio, en los últimos 2 s). Por debajo no hay pulso; por encima suele ser movimiento.
- Si el IR con dedo se acerca a 262143 (el máximo de 18 bits), el ADC se satura: baja `LED_IR_PA` y `LED_RED_PA`. Si la amplitud es muy pequeña, súbelos.

Los valores por defecto son provisionales, igual que los umbrales de calidad del Analyzer: este control local solo decide si empezar o descartar la captura. El control de calidad que cuenta lo hace el Analyzer sobre los 5 minutos completos.

---

## 7. Preparar el servidor

En el equipo del servidor, desde la raíz del repositorio:

```bash
docker compose up -d --wait
docker compose exec -T db psql -U pulseppg -d pulseppg \
  -c "INSERT INTO devices (device_key) VALUES ('pi-lab-1') ON CONFLICT (device_key) DO NOTHING"
```

Usa la misma clave que pusiste en `DEVICE_KEY`. La Pi no toca la base de datos: si el dispositivo no está dado de alta, el Ingestor responde `unknown_device` y el LCD muestra `Disp. no regist.`.

Mosquitto publica el puerto 1883 del servidor (`MQTT_PORT` en `.env`). Abre ese puerto en el cortafuegos del servidor para la red local. Para comprobar desde la Pi que llega:

```bash
sudo apt install -y mosquitto-clients
mosquitto_pub -h IP_DEL_SERVIDOR -t ppg/pi-lab-1/status -m online
```

y en el servidor, `docker compose logs ingestor` debe mostrar `status de pi-lab-1: online`.

---

## 8. Prueba completa sin sensor (opcional)

Antes de usar el sensor puedes comprobar la cadena Pi → MQTT → Ingestor → Analyzer → API con una captura sintética. `--simulate` no usa sensor, LCD ni buzzer: genera la señal, la envía con la misma lógica que la captura real y sale.

```bash
set -a; . /etc/pulseppg/pulseppg.env; set +a
CAPTURE_SECONDS=90 SPOOL_DIR=/tmp/pulseppg-sim CODE_SECONDS=1 \
  /opt/pulseppg/venv/bin/python /opt/pulseppg/pulseppg_node.py --simulate
```

Tarda unos 100 s y termina con `display: CODIGO: XXXXXX`. El Analyzer necesita al menos 60 s de señal, por eso 90. La sesión queda en la base con tu `DEVICE_KEY` y se consulta en `http://IP_DEL_SERVIDOR:8000/sesion/XXXXXX`.

---

## 9. Activar el servicio

```bash
sudo cp pulseppg.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pulseppg
journalctl -u pulseppg -f
```

El log debe mostrar `conectado al broker MQTT` y `display: PulsePPG listo | Coloca el dedo`. El servicio arranca solo al encender la Pi y se reinicia si falla.

| Acción | Comando |
|--------|---------|
| Ver el log | `journalctl -u pulseppg -f` |
| Reiniciar tras cambiar `pulseppg.env` | `sudo systemctl restart pulseppg` |
| Detener | `sudo systemctl stop pulseppg` |
| Capturas pendientes de ACK | `ls /var/lib/pulseppg/pending` |

---

## 10. Qué ve el usuario

| Momento | LCD | Buzzer |
|---------|-----|--------|
| Reposo | `PulsePPG listo` / `Coloca el dedo` | |
| Dedo detectado (2 s seguidos de señal válida) | `Capturando 5:00` y cuenta atrás | un pitido |
| Señal degradada más de 1 s | `Senal perdida.` / `Reintentando...`, y vuelve a reposo | pitido largo |
| Fin de los 5 minutos | `Prueba completada` / `Retira el dedo` | dos pitidos |
| Al retirar el dedo | `Enviando...` | |
| ACK recibido | `CODIGO: XXXXXX` / `Consulta en web` | un pitido |
| Sin ACK tras 3 intentos de 10 s | `Sin conexion.` / `Se reintentara` | |

El HD44780 del LCD no tiene acentos ni ñ, por eso los textos van sin ellos.

Sin ACK, la captura se queda en `/var/lib/pulseppg/pending` y el nodo la reintenta cada 5 minutos mientras está en reposo, aunque la Pi se reinicie. Cuando por fin llega el ACK, muestra el código. El reenvío lleva el mismo archivo y el mismo `file_hash`, y el Ingestor devuelve la sesión original sin duplicarla. Las capturas pendientes se envían de la más antigua a la más nueva.

---

## 11. Problemas frecuentes

- **`i2cdetect` no muestra `57`.** Cableado SDA/SCL invertido, sensor sin 3.3 V o I2C sin activar (`ls /dev/i2c-1`).
- **`PART_ID=0x.. ; se esperaba 0x15`.** En `0x57` hay otro chip; muchos clones del módulo son MAX30100, que no es compatible con este programa.
- **El nodo nunca empieza a capturar.** Mira los valores con `check_sensor.py` y ajusta los umbrales `FINGER_*` (§6).
- **`Senal perdida` constantemente.** Dedo que se mueve o aprieta demasiado, luz ambiente directa sobre el sensor, o `FINGER_PI_MAX` demasiado bajo.
- **`Sin conexion.`.** IP o puerto del servidor, cortafuegos, o Mosquitto parado (`docker compose ps`).
- **`Disp. no regist.`.** Falta el `INSERT INTO devices` del §7 con la misma `DEVICE_KEY`. La captura sigue pendiente y se enviará en el siguiente reintento.
- **`Captura invalida`.** El Ingestor rechazó el archivo; queda en `/var/lib/pulseppg/rejected` y `docker compose logs ingestor` dice por qué.
- **`Sin hora (NTP)`.** La Pi no tiene red o no ha sincronizado la hora (§4.2).

---

## 12. Actualizar el programa

Tras cambiar los archivos en el repositorio:

```bash
cd ~/pulseppg-stress-platform && git pull
cd raspberry-pi
sudo cp pulseppg_node.py max30102.py check_sensor.py requirements.txt /opt/pulseppg/
sudo /opt/pulseppg/venv/bin/pip install -r /opt/pulseppg/requirements.txt
sudo systemctl restart pulseppg
```
