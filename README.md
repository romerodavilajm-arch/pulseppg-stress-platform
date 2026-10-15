# pulseppg-stress-platform

Plataforma IoT + BigData para la adquisición de señal fotopletismográfica (PPG), análisis de variabilidad de frecuencia cardíaca (HRV) y clasificación de estrés por reglas fisiológicas.

---

## 1. Alcance

El prototipo incluye:

- Captura de señal PPG con un sensor MAX30102 en una Raspberry Pi 5.
- Control de calidad local durante la captura.
- Envío de la sesión a un servidor mediante MQTT.
- Ingesta de sesiones y muestras crudas en PostgreSQL + TimescaleDB.
- Generación del código de sesión en la base de datos.
- Procesamiento independiente de las sesiones mediante analizadores que leen los datos crudos y generan sus propios resultados.
- Procesamiento de señales con pandas, NumPy, SciPy y HeartPy.
- Análisis HRV en dominio temporal y frecuencial sobre la señal completa de 5 minutos.
- Métricas de calidad de señal y estimación de frecuencia respiratoria.
- Clasificación de estrés por reglas fisiológicas sobre las métricas HRV.
- Web responsive para consulta del usuario por código, sin caducidad.
- Web de administración con tabla global de sesiones.

No incluye uso clínico, diagnóstico médico, validación con población amplia, ni despliegue en nube. El sistema no compara al usuario contra una población de referencia, sino contra umbrales generales de HRV para adultos sanos en reposo.

---

## 2. Descripción del problema

La medición de estrés suele requerir equipos clínicos costosos o cuestionarios subjetivos. Existen sensores PPG de bajo costo capaces de capturar la señal cardíaca, pero obtener métricas útiles de ellos exige resolver varios problemas en cadena:

- La señal PPG es sensible al movimiento, la presión del dedo y la perfusión, por lo que la calidad varía muestra a muestra.
- Las métricas HRV requieren procesamiento de señales (filtrado, detección de picos, análisis espectral), no solo leer el sensor.
- Un nodo aislado no permite consultar resultados desde otro dispositivo ni acumular historial.
- Escalar de un sensor a varios, o de una métrica a varias, suele obligar a rediseñar el sistema completo.

El problema, entonces, no es solo "medir estrés", sino construir una arquitectura que capture, transporte, procese y presente esa señal de forma desacoplada y escalable.

---

## 3. Estrategia de solución

El sistema se organiza en cinco contenedores que se comunican por una red Docker, más un nodo físico que se conecta por red local:

| Componente | Rol |
|------------|-----|
| Mosquitto | Broker MQTT, transporte de mensajes entre nodo y servidor |
| PostgreSQL + TimescaleDB | Almacenamiento y generación de códigos de sesión |
| Ingestor | Persistencia de sesiones y muestras, confirmación al nodo |
| Analyzer | Pipeline ETL, cálculo de métricas fisiológicas y clasificación |
| API | Endpoints JSON y páginas HTML para admin y usuario |

La estrategia sigue cinco decisiones de diseño:

- **Captura desacoplada.** La Raspberry Pi solo publica mensajes MQTT. No conoce el resto del sistema, no genera códigos de sesión, no calcula identificadores: solo captura, comprime, hashea y publica. Añadir más nodos no requiere tocar el servidor.
- **Broker MQTT como bus.** Mosquitto transporta todos los mensajes entre el nodo, el Ingestor, el Analyzer y cualquier suscriptor futuro. La jerarquía de topics hace de espacio de nombres unificado.
- **Analizadores independientes.** Cada Analyzer descubre las sesiones disponibles mediante su propio mecanismo, lee los datos crudos y genera sus propios resultados. La base central no coordina el trabajo de los analizadores.
- **Fuente de verdad para la ingesta.** PostgreSQL + TimescaleDB almacena dispositivos, sesiones y señal cruda, y genera los códigos de sesión. No conoce analizadores ni mantiene una cola central de análisis.
- **Servicios pequeños con responsabilidad única.** Cada contenedor hace una sola cosa. El Ingestor persiste, el Analyzer analiza, la API consulta. Ninguno invade el rol de otro.

El resultado es un flujo donde la señal PPG nace en el sensor, se transporta por MQTT, se analiza sobre la señal completa de 5 minutos mediante un pipeline ETL de tres fases (obtención, transformación, análisis) y se consulta desde una web por un código de seis posiciones generado por la base de datos.

---

## 4. Decisiones de simplificación

Respecto a versiones previas del diseño, el prototipo adopta tres simplificaciones que reducen piezas móviles sin recortar el análisis:

- **Análisis sobre la señal completa de 5 minutos.** No se divide en ventanas. El control de calidad es global: si la señal completa no supera los umbrales de SQI, PI o SNR, la sesión se marca como `low_quality` y no se clasifica. Cinco minutos es la ventana estándar para HRV de corto plazo (Task Force, 1996), lo que permite reportar dominio temporal y frecuencial sin asteriscos ni ajustes ad hoc.
- **Código de sesión generado por la base de datos.** La Pi no genera códigos ni maneja colisiones. La base de datos produce un código Base32 de 6 caracteres al insertar la sesión y lo devuelve en el ACK. Se elimina el riesgo de códigos duplicados en el borde, se elimina la necesidad de un prefijo por dispositivo y se garantiza que el código que ve el usuario es siempre el que está persistido.
- **Códigos sin caducidad.** Una sesión es consultable en todo momento mientras exista, no solo durante las primeras 24 horas. Simplifica los mensajes de error y elimina una constante duplicada entre el nodo y la API.

---

## 5. Escalabilidad

La arquitectura escala en tres ejes sin rediseño:

- **Nuevos sensores.** Añadir un sensor nuevo (por ejemplo, respuesta galvánica de la piel) implica crear una tabla nueva con `session_id` como clave foránea y añadir una rama al Ingestor que escuche el topic correspondiente. Los analizadores existentes no cambian.
- **Nuevos analizadores.** Desplegar un contenedor con su propia lógica de descubrimiento, procesamiento y persistencia de resultados. El esquema central no necesita conocer su existencia.
- **Más réplicas.** Cada familia de análisis puede escalar de acuerdo con su propio mecanismo de descubrimiento y procesamiento. La base central no coordina las réplicas.

Un clasificador de machine learning futuro puede implementarse como un Analyzer independiente, con sus propias tablas de resultados y mecanismo de descubrimiento, sin modificar el esquema central.

---

## Prueba local de la base de datos

`compose.yaml` levanta PostgreSQL 16 + TimescaleDB con el esquema de `init.sql`, Mosquitto, el Ingestor, el Analyzer HRV y la API.

```bash
cp .env.example .env            # opcional: credenciales y puerto
docker compose up -d --wait     # construye Ingestor, Analyzer y API y espera a que todo esté sano
docker compose exec -T db psql -U pulseppg -d pulseppg -v ON_ERROR_STOP=1 < tests/db/smoke_test.sql
```

La prueba termina con `SMOKE TEST OK` y no deja datos (corre en una transacción con `ROLLBACK`). `init.sql` solo se ejecuta con el volumen vacío; para recrear la base desde cero: `docker compose down -v`.

Si la base ya existía antes del índice que evita sesiones duplicadas, aplícalo sin borrar datos (el Ingestor lo necesita):

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg -v ON_ERROR_STOP=1 < migrations/001_uniq_sessions_device_file.sql
```

## Prueba local del Ingestor (sin Raspberry Pi)

`tests/ingestor/fake_pi.py` simula la Pi: genera 5 minutos de señal a 50 Hz, la comprime en JSONL + gzip, calcula su SHA-256, la publica en `ppg/{device_key}/session` y espera el ACK. `tests/ingestor/e2e.sh` registra el dispositivo `test-pi`, envía una sesión válida, comprueba en la BD que existen la sesión y sus 15 000 muestras, verifica los ACK de error por hash incorrecto y por dispositivo no registrado, y comprueba que reenviar la misma sesión devuelve el mismo `session_code` sin duplicarla.

```bash
docker compose up -d --wait
./tests/ingestor/e2e.sh
```

La prueba termina con `INGESTOR E2E OK (session_code=XXXXXX)`. A diferencia de la prueba de humo, deja la sesión de prueba en la base. Los logs del Ingestor se ven con `docker compose logs -f ingestor`.

Formato del mensaje en `ppg/{device_key}/session`:

```json
{
  "started_at": "2026-01-01T10:00:00+00:00",
  "finished_at": "2026-01-01T10:05:00+00:00",
  "sample_count": 15000,
  "file_hash": "<sha256 hex del archivo .jsonl.gz>",
  "data": "<archivo .jsonl.gz en base64>"
}
```

Cada línea del JSONL es una muestra `{"time": "<ISO 8601>", "ir": <int>, "red": <int>}`. El ACK de error lleva `error` con `invalid_payload`, `unknown_device` o `internal`. Los dispositivos deben estar dados de alta en `devices` antes de enviar. Si un dispositivo reenvía un archivo con el mismo `file_hash` (por ejemplo, porque no le llegó el ACK), el Ingestor no lo guarda otra vez y responde con el `session_id` y `session_code` de la sesión original.

## Prueba local del Analyzer HRV (sin Raspberry Pi)

El Analyzer (`analyzer/`) descubre las sesiones por el aviso MQTT `analysis/{session_id}/request` y, como respaldo, sondeando cada `ANALYZER_POLL_SECONDS` (60 s por defecto) las sesiones sin resultado. Lee `raw_ppg`, ejecuta el pipeline de `docs/flujo.md` §5 (`analyzer/pipeline.py`) y guarda sus resultados en su propio esquema `hrv`, que crea al arrancar (`analyzer/schema.sql`):

- `hrv.metrics`: una fila por sesión y métrica (`bpm`, `sdnn`, `rmssd`, `pnn50`, `nn50`, `ibi_mean`, `lf`, `hf`, `lf_hf_ratio`, `lf_nu`, `hf_nu`, `total_power`, `sqi`, `perfusion_index`, `snr_db`, `respiratory_rate`).
- `hrv.stress_results`: `label` (`stress`, `no_stress`, `low_quality`), `score` (reglas cumplidas / 4; `NULL` si es `low_quality`) y `rules_met`.
- `hrv.failures`: sesiones que no se pudieron analizar y por qué (por ejemplo, señal de menos de 60 s). Los fallos transitorios se reintentan hasta 3 veces.

Al terminar publica `analysis/{session_id}/complete` con la etiqueta y el score. No modifica `sessions` ni `raw_ppg`.

```bash
docker compose up -d --wait
./tests/analyzer/e2e.sh
```

La prueba envía tres sesiones simuladas de 5 minutos con `fake_pi.py --profile`: `relaxed` (HRV alta) debe salir `no_stress`, `stressed` (HRV baja, LF dominante) `stress` y `noisy` (ruido que tapa el pulso) `low_quality`. Termina con `ANALYZER E2E OK` y una tabla con las métricas de cada sesión. Los logs se ven con `docker compose logs -f analyzer`.

Los umbrales de calidad no están fijados en la documentación; los valores por defecto son provisionales y se pueden ajustar con `HRV_SQI_MIN` (0.86), `HRV_PI_MIN` (0.1 %) y `HRV_SNR_MIN_DB` (0 dB) al calibrar con capturas reales.

## API y web (sin Raspberry Pi)

La API (`api/`, FastAPI + Jinja2 + Bootstrap por CDN) escucha en el puerto 8000 (`API_PORT`). Solo lee: los metadatos de `sessions` y `devices` y los resultados que el Analyzer guarda en su esquema `hrv`. No escribe en ninguna tabla.

| Ruta | Qué devuelve |
|------|--------------|
| `/` | Formulario para consultar por código |
| `/sesion/{code}` | Resultado para el usuario (fecha, duración, etiqueta, índice, BPM, SDNN, RMSSD, pNN50, LF/HF, frecuencia respiratoria) |
| `/api/sessions/{code}` | Lo mismo en JSON, con las 16 métricas |
| `/admin` | Tabla global con filtros por dispositivo, rango de fechas y resultado |
| `/admin/sesiones/{id}` y `/api/admin/sessions[/{id}]` | Detalle y tabla en HTML o JSON |
| `/api/docs` | Documentación OpenAPI |

El código se acepta en minúsculas y con espacios o guiones. Un código inexistente devuelve 404 con "Código no encontrado". La API distingue el estado del análisis (`analysis.status`), que calcula al consultar y no se guarda en la base:

- `completed`: hay resultado del Analyzer (`stress`, `no_stress` o `low_quality`).
- `pending`: la sesión existe pero aún no se ha analizado.
- `failed`: el Analyzer no pudo analizarla (fallo permanente o agotó `HRV_MAX_ATTEMPTS` intentos, 3 por defecto, el mismo valor que usa el Analyzer).

En las sesiones `low_quality` no se muestran métricas ni índice, porque las métricas HRV de una señal mala no tienen sentido. La administración enseña solo las de calidad (`sqi`, `perfusion_index`, `snr_db`), que explican el descarte. Las fechas se muestran y filtran por día en la zona `API_TIMEZONE` (`America/Mexico_City` por defecto).

```bash
docker compose up -d --wait
./tests/api/e2e.sh
```

La prueba detiene el Analyzer, envía una sesión y comprueba que la API la da por pendiente; lo vuelve a arrancar, envía una sesión con estrés, otra con ruido y una sin muestras, y comprueba el JSON y las páginas de usuario y de administración (etiquetas, ocultación de métricas en `low_quality`, análisis fallido, código no encontrado y filtros). Termina con `API E2E OK` y los enlaces para abrir el resultado y la administración en el navegador. Deja las sesiones de prueba en la base.

## Cuatro Pi simuladas (casos manuales)

`compose.sim.yaml` añade cuatro contenedores, `pi1` a `pi4`, con la misma imagen (la del Ingestor, construida una vez como `pulseppg-pi-sim`) y el mismo script, `tests/ingestor/fake_pi.py`, montado en `/sim`. Los contenedores no envían nada al arrancar: quedan en espera para que entres a cada uno y lances el script con sus parámetros. Se combina con `compose.yaml`, así que `docker compose up` sin `-f` sigue arrancando solo los servicios normales.

```bash
docker compose -f compose.yaml -f compose.sim.yaml up -d --wait
```

Cada Pi debe estar dada de alta en `devices` antes de enviar (la Pi no toca la base de datos):

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg \
  -c "INSERT INTO devices (device_key) VALUES ('pi-1'), ('pi-2'), ('pi-3'), ('pi-4') ON CONFLICT (device_key) DO NOTHING"
```

Para entrar a un contenedor y ejecutar el script a mano:

```bash
docker compose -f compose.yaml -f compose.sim.yaml exec pi1 bash
python fake_pi.py --device pi-1 --profile relaxed
```

Los cuatro casos sugeridos, uno por contenedor (también se pueden lanzar sin entrar, con `exec` y el comando completo):

| Contenedor | Comando | Resultado esperado |
|------------|---------|--------------------|
| `pi1` | `python fake_pi.py --device pi-1 --profile relaxed` | `no_stress` (HRV alta) |
| `pi2` | `python fake_pi.py --device pi-2 --profile stressed` | `stress` (HRV baja, LF dominante) |
| `pi3` | `python fake_pi.py --device pi-3 --profile noisy` | `low_quality` (el ruido tapa el pulso) |
| `pi4` | `python fake_pi.py --device pi-4 --profile sine` | `stress` (ritmo perfectamente regular, sin variabilidad) |

El script imprime el ACK con el `session_code`, que se consulta en `http://localhost:8000/sesion/{code}` cuando el Analyzer termina. Otros parámetros útiles: `--start` (fecha fija, para simular un reenvío con el mismo hash), `--corrupt-hash` con `--expect error` (ACK de error) y `--samples` (duración; 15 000 muestras son 5 minutos a 50 Hz). `python fake_pi.py -h` los lista todos.

Para detener todo, incluidos los simuladores, usa los mismos archivos: `docker compose -f compose.yaml -f compose.sim.yaml down`.

## Raspberry Pi real

La carpeta [`raspberry-pi/`](raspberry-pi/) contiene el programa del nodo (Raspberry Pi 5 + MAX30102, LCD 16x2 y buzzer) y [`raspberry-pi/setup.md`](raspberry-pi/setup.md), la guía para instalar Raspberry Pi OS Lite, cablear el sensor, configurar el nodo como servicio systemd y darlo de alta en el servidor. Cumple el mismo contrato MQTT que `fake_pi.py`, así que el servidor no cambia.

## Documentación

Consulta [`docs/flujo.md`](docs/flujo.md) para el recorrido completo de los datos, las decisiones de diseño y el detalle del pipeline ETL.

## Licencia

Este proyecto se distribuye bajo licencia MIT. Consulta el archivo [LICENSE](LICENSE) para más detalles.

## Autor

Desarrollado por un estudiante de Ingeniería en Sistemas Computacionales del Instituto Tecnológico de Querétaro, para el 1er Congreso Internacional de Innovación Tecnológica "Código sin fronteras".
