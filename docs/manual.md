# Manual de uso de PulsePPG Stress Platform

Guía práctica para poner en marcha, usar, probar y ampliar el prototipo. Para el porqué de cada decisión y el detalle del pipeline consulta [`flujo.md`](flujo.md); para instalar el nodo físico, [`raspberry-pi/setup.md`](../raspberry-pi/setup.md).

---

## 1. Qué hay en el sistema

| Pieza | Dónde está | Qué hace |
|-------|-----------|----------|
| `db` (PostgreSQL 16 + TimescaleDB) | `compose.yaml`, `init.sql` | Guarda `devices`, `sessions` y `raw_ppg`; genera el código de sesión |
| `mosquitto` | `compose.yaml`, `mosquitto/` | Broker MQTT en el puerto 1883, sin autenticación |
| `ingestor` | `ingestor/` | Recibe las sesiones de `ppg/+/session`, las guarda y responde el ACK con el código |
| `analyzer` (HRV) | `analyzer/` | Calcula métricas HRV y la clasificación de estrés; guarda en su propio esquema `hrv` |
| `api` | `api/` | Web de usuario (`/`), administración (`/admin`) y JSON en el puerto 8000. Solo lee |
| Nodo Raspberry Pi | `raspberry-pi/` | Captura 5 min con el MAX30102 y publica la sesión por MQTT |
| Pi simuladas | `compose.sim.yaml`, `tests/ingestor/fake_pi.py` | Sustituyen a la Pi real para probar |

El recorrido es: la Pi publica la sesión, el Ingestor la guarda y devuelve el código, el Analyzer la procesa y la API muestra el resultado.

---

## 2. Requisitos

- Docker con el plugin `docker compose`.
- Para la Pi real: Raspberry Pi 5, sensor MAX30102 y, opcionalmente, LCD 16x2 y buzzer (ver `raspberry-pi/setup.md`).
- La Pi y el servidor en la misma red local, con el puerto 1883 del servidor abierto.

---

## 3. Arrancar y detener el servidor

Desde la raíz del repositorio:

```bash
cp .env.example .env          # opcional: credenciales, puertos y umbrales
docker compose up -d --wait   # construye Ingestor, Analyzer y API y espera a que estén sanos
docker compose ps             # estado de los contenedores
```

Para detener:

```bash
docker compose down           # conserva la base y los mensajes de Mosquitto
docker compose down -v        # además borra los volúmenes (base desde cero)
```

`init.sql` solo se ejecuta cuando el volumen de la base está vacío. Si la base viene de una versión anterior al índice que evita sesiones duplicadas, aplica la migración sin borrar datos:

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg -v ON_ERROR_STOP=1 < migrations/001_uniq_sessions_device_file.sql
```

### Variables de `.env`

| Variable | Por defecto | Para qué sirve |
|----------|-------------|----------------|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `pulseppg` | Credenciales de la base |
| `POSTGRES_PORT` | `5432` | Puerto de la base en el equipo anfitrión |
| `MQTT_PORT` | `1883` | Puerto de Mosquitto en el equipo anfitrión |
| `ANALYZER_POLL_SECONDS` | `60` | Cada cuánto el Analyzer busca sesiones sin resultado |
| `HRV_MAX_ATTEMPTS` | `3` | Intentos antes de dar un análisis por fallido (lo usan el Analyzer y la API) |
| `HRV_SQI_MIN`, `HRV_PI_MIN`, `HRV_SNR_MIN_DB` | `0.86`, `0.1`, `0` | Umbrales de calidad (provisionales, se calibran con capturas reales) |
| `API_PORT` | `8000` | Puerto de la web |
| `API_TIMEZONE` | `America/Mexico_City` | Zona horaria para mostrar y filtrar fechas |

Tras cambiar `.env`, vuelve a ejecutar `docker compose up -d --wait` para que los contenedores tomen los valores nuevos.

---

## 4. Dar de alta un dispositivo

Cada nodo se identifica por su `device_key`. El Ingestor rechaza con `unknown_device` cualquier sesión de una clave que no esté en `devices`, así que el alta va antes del primer envío:

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg \
  -c "INSERT INTO devices (device_key) VALUES ('pi-lab-1') ON CONFLICT (device_key) DO NOTHING"
```

Para ver los dispositivos y cuándo se conectaron por última vez:

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg -c "SELECT device_key, last_seen FROM devices ORDER BY device_key"
```

---

## 5. Hacer una medición

### Con la Raspberry Pi real

Sigue `raspberry-pi/setup.md` una vez para instalar el nodo. Después, el uso diario es:

1. El servidor está arrancado y el dispositivo dado de alta.
2. La persona coloca el dedo en el sensor. El nodo detecta el dedo y captura 5 minutos; si la señal se pierde, avisa y reinicia la captura desde cero.
3. Al terminar, el nodo envía la sesión y el LCD muestra `CODIGO: XXXXXX`.
4. La persona consulta ese código en `http://IP_DEL_SERVIDOR:8000/`.

Si el servidor no responde, el nodo guarda la sesión y la reintenta cada `RETRY_SECONDS` (5 min); el código aparece cuando llega el ACK. Comandos útiles en la Pi: `journalctl -u pulseppg -f` (log), `sudo systemctl restart pulseppg` (tras cambiar `/etc/pulseppg/pulseppg.env`), `sudo systemctl stop pulseppg` (detener).

### Sin Raspberry Pi (simuladores)

`compose.sim.yaml` añade cuatro Pi simuladas (`pi1` a `pi4`) que esperan a que lances `fake_pi.py` dentro de ellas:

```bash
docker compose -f compose.yaml -f compose.sim.yaml up -d --wait
docker compose exec -T db psql -U pulseppg -d pulseppg \
  -c "INSERT INTO devices (device_key) VALUES ('pi-1'), ('pi-2'), ('pi-3'), ('pi-4') ON CONFLICT (device_key) DO NOTHING"
docker compose -f compose.yaml -f compose.sim.yaml exec pi1 python fake_pi.py --device pi-1 --profile relaxed
```

Perfiles disponibles: `relaxed` (sale `no_stress`), `stressed` (`stress`), `noisy` (`low_quality`) y `sine` (`stress`, ritmo sin variabilidad). `python fake_pi.py -h` lista el resto de opciones (`--samples`, `--start`, `--corrupt-hash`, `--expect`). Para parar todo, usa los mismos `-f` con `down`.

---

## 6. Consultar los resultados

| Ruta | Para quién | Qué muestra |
|------|-----------|-------------|
| `/` | Usuario | Formulario para escribir el código |
| `/sesion/{code}` | Usuario | Fecha, duración, etiqueta, índice y métricas principales |
| `/admin` | Administración | Tabla de todas las sesiones con filtros por dispositivo, fechas y resultado |
| `/admin/sesiones/{id}` | Administración | Detalle de una sesión con todas sus métricas |
| `/api/sessions/{code}`, `/api/admin/sessions[/{id}]` | Integraciones | Lo mismo en JSON |
| `/api/docs` | Desarrollo | Documentación OpenAPI |

El estado del análisis puede ser `pending` (aún no se analiza; recarga más tarde), `completed` (con etiqueta `stress`, `no_stress` o `low_quality`) o `failed` (el Analyzer no pudo analizarla). En `low_quality` no se muestran métricas: hay que repetir la medición con mejor contacto del dedo. La web no se actualiza sola; se recarga a mano. La administración no tiene autenticación en el prototipo.

---

## 7. Comprobar que todo funciona

Con el servidor arrancado:

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg -v ON_ERROR_STOP=1 < tests/db/smoke_test.sql   # SMOKE TEST OK
./tests/ingestor/e2e.sh    # INGESTOR E2E OK
./tests/analyzer/e2e.sh    # ANALYZER E2E OK
./tests/api/e2e.sh         # API E2E OK
```

Las pruebas e2e dejan sesiones de prueba en la base (la de humo no). GitHub Actions (`.github/workflows/ci.yml`) ejecuta las cuatro en cada PR y en cada push a `main`.

Para ver qué está pasando: `docker compose logs -f ingestor`, `docker compose logs -f analyzer` o `docker compose logs -f api`.

---

## 8. Aumentar el número de sensores (Raspberry Pi)

El servidor no necesita cambios para recibir más nodos: el Ingestor se suscribe a `ppg/+/session` y `ppg/+/status`, así que atiende a cualquier `device_key` que esté dado de alta. Añadir una Pi es solo configuración:

1. **Elige una clave única** para el nodo, por ejemplo `pi-lab-2`. La clave identifica al dispositivo en la base y en los topics MQTT, y también forma su identificador de cliente MQTT (`pulseppg-node-{DEVICE_KEY}`). Dos nodos con la misma clave mezclarían sus sesiones y se desconectarían mutuamente del broker.
2. **Dala de alta** en `devices` (sección 4).
3. **Instala el nodo** siguiendo `raspberry-pi/setup.md` y, en `/etc/pulseppg/pulseppg.env`, pon `DEVICE_KEY=pi-lab-2` y `MQTT_HOST` con la IP del servidor.
4. **Verifica** con `pulseppg_node.py --simulate` (setup.md §8) o mirando en el log del Ingestor `status de pi-lab-2: online`.

Lo que no cambia: el Ingestor, el Analyzer, la API y el esquema. Las sesiones de todos los nodos aparecen en `/admin` y se pueden filtrar por dispositivo.

Para probar varios nodos sin hardware, los cuatro simuladores de `compose.sim.yaml` ya cubren el caso. Si se necesitan más, se añade otro servicio `pi5` con `<<: *pi-sim` y su `container_name` en ese archivo, y se da de alta su clave.

Ten en cuenta que, en el prototipo, hay un solo Ingestor y procesa los mensajes de uno en uno; con unos pocos nodos que envían una sesión cada 5 minutos no es un límite práctico.

### Otro tipo de sensor

Si lo que se añade es otra señal (por ejemplo, respuesta galvánica de la piel), la arquitectura prevé (README §5, `flujo.md` §10) que use su propio topic y su propia tabla con `session_id` como clave foránea, y que el Ingestor gane una rama para ese topic. Eso sí es un cambio de código y no está implementado; los analizadores existentes no se tocan.

---

## 9. Aumentar los contenedores de análisis

Hay dos casos distintos.

### Añadir un analizador nuevo (otra familia de análisis)

Es el caso previsto por la arquitectura y no afecta a lo que ya funciona. Un analizador nuevo, por ejemplo un clasificador de machine learning, se añade como otro contenedor siguiendo el mismo contrato que el Analyzer HRV (`flujo.md` §5 y §6):

1. **Su propio código y Dockerfile**, en una carpeta nueva (por ejemplo `analyzer-ml/`), con un servicio nuevo en `compose.yaml` conectado a la red `pulseppg`.
2. **Su propio esquema de resultados**, creado por el propio analizador al arrancar, como hace `analyzer/schema.sql` con `hrv`. No se toca `init.sql` ni las tablas centrales.
3. **Su propio descubrimiento.** Puede suscribirse a `analysis/+/request` (el aviso que publica el Ingestor tras cada sesión), sondear `sessions` buscando las que no tienen resultado en sus tablas, o ambas cosas, como el HRV.
4. **Solo lectura del plano central.** Lee `sessions` y `raw_ppg`; nunca los modifica, ni genera códigos, ni borra muestras.
5. **Identificador de cliente MQTT propio**, distinto de `pulseppg-analyzer-hrv`, y un `container_name` propio si se fija uno.
6. **Opcional:** publicar `analysis/{session_id}/complete` al terminar.

Con eso, el Ingestor, la base central y el Analyzer HRV siguen igual. Lo único que no aparece solo es la presentación: la API actual lee únicamente el esquema `hrv`, así que para ver los resultados del nuevo analizador en la web habría que añadir esa lectura a `api/`.

Para comprobarlo sin afectar al resto, se puede levantar solo el servicio nuevo (`docker compose up -d --wait nombre-del-servicio`) y reenviar sesiones con los simuladores; las pruebas e2e existentes deben seguir pasando.

### Más réplicas del Analyzer HRV

La arquitectura deja que cada familia de análisis escale con su propio mecanismo (`flujo.md` §10), pero **el Analyzer HRV actual está hecho para correr como una sola instancia**. Hoy no basta con `docker compose up --scale analyzer=2`, por tres motivos que se ven en el código:

- `compose.yaml` fija `container_name: pulseppg-analyzer`, y Docker no permite dos contenedores con el mismo nombre.
- `analyzer/analyzer.py` usa el identificador MQTT fijo `pulseppg-analyzer-hrv`; Mosquitto desconecta a un cliente cuando otro se conecta con el mismo identificador, así que dos réplicas se echarían una a otra.
- Las réplicas no se reparten el trabajo: cada una consulta las sesiones sin resultado y las analizaría todas. Los resultados no se corromperían, porque la escritura es idempotente (`ON CONFLICT` por sesión), pero el trabajo se duplicaría y los intentos fallidos se contarían dos veces.

Para escalar el HRV haría falta, como mínimo, quitar el `container_name`, dar a cada réplica un identificador MQTT distinto y que cada réplica reclame la sesión antes de procesarla (por ejemplo, con un bloqueo en la base dentro del esquema `hrv`). Es un cambio dentro del propio Analyzer, sin tocar la base central ni el Ingestor, pero no está implementado. Con el volumen del prototipo (una sesión de 5 minutos por nodo) una instancia es suficiente.

---

## 10. Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---------|----------------|-----------|
| La Pi o `fake_pi.py` recibe `unknown_device` | La clave no está en `devices` | Darla de alta (sección 4) con la misma clave |
| ACK de error `invalid_payload` | Payload incompleto o hash que no coincide | Revisar el log del Ingestor |
| La web dice que el análisis está pendiente | El Analyzer no la ha procesado aún o estaba parado | Esperar hasta `ANALYZER_POLL_SECONDS` y recargar; ver `docker compose logs analyzer` |
| Resultado `failed` | La sesión no se pudo analizar (por ejemplo, menos de 60 s de señal) | Ver el motivo en `/admin/sesiones/{id}` o en `hrv.failures` |
| Muchas sesiones `low_quality` con el sensor real | Mal contacto del dedo o umbrales sin calibrar | Repetir la medición; calibrar `HRV_*_MIN` en `.env` |
| La Pi no conecta al broker | Puerto 1883 cerrado o `MQTT_HOST` incorrecto | Probar con `mosquitto_pub` desde la Pi (setup.md §7) |
| Error del Ingestor por el índice de duplicados | Base creada antes de la migración 001 | Aplicar `migrations/001_uniq_sessions_device_file.sql` (sección 3) |

---

## 11. Límites del prototipo

No es un dispositivo clínico ni hace diagnóstico. La clasificación compara contra umbrales generales de HRV para adultos sanos en reposo. Mosquitto y la administración no tienen autenticación, así que el sistema debe usarse en una red local de confianza. Las muestras crudas (`raw_ppg`) se borran a los 30 días; las sesiones y los resultados se conservan.
