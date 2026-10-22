# Pruebas de PulsePPG Stress Platform

Cómo levantar el servidor y probar cada componente sin Raspberry Pi: la base de datos, el Ingestor, el Analyzer HRV y la API, más los envíos manuales con `fake_pi.py`. Todas las rutas son relativas a la raíz del repositorio. Para el uso diario consulta [`manual.md`](manual.md); para el diseño, [`flujo.md`](flujo.md).

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

La API (`api/`, FastAPI + Jinja2 + Bootstrap servido en local desde `api/static/`) escucha en el puerto 8000 (`API_PORT`). Solo lee: los metadatos de `sessions` y `devices` y los resultados que el Analyzer guarda en su esquema `hrv`. No escribe en ninguna tabla.

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

## Envíos manuales sin Raspberry Pi

`tests/ingestor/fake_pi.py` también sirve para enviar sesiones a mano. Se ejecuta con la imagen del Ingestor, la misma que usan las pruebas e2e, así que no hace falta ningún contenedor ni archivo de compose aparte.

Cada dispositivo debe estar dado de alta en `devices` antes de enviar (la Pi no toca la base de datos):

```bash
docker compose exec -T db psql -U pulseppg -d pulseppg \
  -c "INSERT INTO devices (device_key) VALUES ('pi-1'), ('pi-2'), ('pi-3'), ('pi-4') ON CONFLICT (device_key) DO NOTHING"
```

Para enviar una sesión:

```bash
docker compose run --rm --no-deps -v ./tests:/tests ingestor \
  python /tests/ingestor/fake_pi.py --device pi-1 --profile relaxed
```

Los cuatro casos sugeridos:

| Dispositivo | Parámetros | Resultado esperado |
|-------------|-----------|--------------------|
| `pi-1` | `--device pi-1 --profile relaxed` | `no_stress` (HRV alta) |
| `pi-2` | `--device pi-2 --profile stressed` | `stress` (HRV baja, LF dominante) |
| `pi-3` | `--device pi-3 --profile noisy` | `low_quality` (el ruido tapa el pulso) |
| `pi-4` | `--device pi-4 --profile sine` | `stress` (ritmo perfectamente regular, sin variabilidad) |

El script imprime el ACK con el `session_code`, que se consulta en `http://localhost:8000/sesion/{code}` cuando el Analyzer termina. Otros parámetros útiles: `--start` (fecha fija, para simular un reenvío con el mismo hash), `--corrupt-hash` con `--expect error` (ACK de error) y `--samples` (duración; 15 000 muestras son 5 minutos a 50 Hz). `--help` los lista todos.
