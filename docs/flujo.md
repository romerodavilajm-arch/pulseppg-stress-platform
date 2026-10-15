# flujo.md

Documento que describe el recorrido de los datos en PulsePPG Stress Platform. Escrito en texto plano, sin diagramas ni código, para consumo directo por herramientas de asistencia como Claude Code. Los detalles de implementación pueden cambiar durante el desarrollo sin invalidar lo aquí descrito; este documento fija conceptos, responsabilidades y contratos entre componentes.

---

## 1. Componentes

Cuatro contenedores principales en una red Docker más un nodo físico. La base de datos central mantiene únicamente el plano de ingesta y los datos crudos. Los analizadores son componentes independientes y no forman parte del esquema central.

**Mosquitto.** Broker MQTT puro (imagen oficial). Puerto 1883. Sin lógica de negocio, sin autenticación en el prototipo. Persiste en volumen Docker.

**PostgreSQL + TimescaleDB.** Única fuente de verdad para dispositivos, sesiones y señal cruda. Contiene `devices`, `sessions` y `raw_ppg`. La base de datos genera `session_code` al insertar una sesión. No conoce analizadores ni mantiene una cola de análisis central. `raw_ppg` tiene retención de 30 días.

**Ingestor.** Python, pocas líneas. Se suscribe a `ppg/+/session`, valida el payload, resuelve el dispositivo, persiste la sesión y las muestras en una transacción, publica el ACK y, si corresponde, notifica que existe una sesión disponible para análisis. No analiza, no genera códigos y no administra estados de análisis.

**Analyzer.** Python. Es un contenedor de procesamiento independiente. Obtiene los datos crudos de una sesión desde PostgreSQL, ejecuta el pipeline ETL con pandas/NumPy/SciPy/HeartPy y genera los resultados de su propio análisis. No crea ni modifica sesiones, dispositivos ni muestras crudas. No genera códigos de sesión. No administra el estado global de la sesión ni una cola central de trabajo. Cada familia de análisis es responsable de su propio mecanismo de descubrimiento y de sus propias tablas de resultados.

**API.** FastAPI + Jinja2 en el puerto 8000. Sirve la web de usuario y la de administrador. Consulta los datos disponibles y no participa en la captura ni en el procesamiento.

**Raspberry Pi 5.** Lee el MAX30102 por I2C, controla display 16x2 y buzzer. Solo publica MQTT y espera ACKs. No genera códigos, no calcula hashes de sesión más allá del SHA-256 del archivo, no conoce la base de datos.

---

## 2. Puesta en marcha

1. **Registrar dispositivos.** Alta en `devices` con `device_key` único. Sin prefijo: el dispositivo se identifica solo por su clave.

2. **Configurar la Pi.** IP del servidor, puerto 1883, `device_key`. `systemctl restart pulseppg`.

3. **Arrancar el servidor.** `docker compose up -d`.

4. **Verificar.** La Pi publica `online` en `ppg/{device_key}/status`. El Ingestor actualiza `last_seen`.

5. **Configurar analizadores.** Cada Analyzer se configura de forma independiente, con sus propias tablas, mecanismo de descubrimiento y reglas de procesamiento. El esquema central no registra analizadores.

---

## 3. Captura en la Pi

**3.1. Detección de dedo.** Lectura a 50 Hz. Umbrales sobre amplitud DC del canal IR, ratio IR/rojo y SQI por ventana corta. Dos segundos consecutivos dentro de umbral equivalen a dedo presente.

**3.2. Inicio.** Estado CAPTURING. Display: "Capturando 5:00".

**3.3. Captura.** Cinco minutos. Cada ciclo lee una muestra, la escribe en JSONL y evalúa calidad cada 500 ms.

**3.4. Señal inválida.** Si la calidad se degrada más de un segundo: buzzer, display "Señal perdida. Reintentando...", descarta el buffer, vuelve a IDLE. No hay reanudación parcial. La captura completa se repite desde cero.

**3.5. Fin.** Señal válida durante 5 minutos completos. Display: "Prueba completada. Retira el dedo."

**3.6. Cierre.** Al retirar el dedo: cierra el JSONL, comprime con gzip, calcula SHA-256 del archivo. **La Pi no genera ningún código.** Solo prepara el payload.

**3.7. Envío.** Publica en `ppg/{device_key}/session` el archivo comprimido en base64, las fechas de inicio y fin, el número de muestras y el hash. Display: "Enviando..."

**3.8. Confirmación.** Espera ACK en `ppg/{device_key}/ack` durante 10 segundos. El ACK contiene el `session_id` y el `session_code` que la base de datos generó al persistir la sesión.

- ACK recibido: display "CODIGO: XXXXXX", vuelve a IDLE tras unos segundos.
- Sin ACK tras tres reintentos: guarda localmente y reintenta cada 5 minutos. Cuando finalmente llegue el ACK, muestra el código generado.

El ACK confirma persistencia en PostgreSQL, no análisis. La disponibilidad de resultados ocurre posteriormente y no bloquea a la Pi. El código que ve el usuario es siempre el que está en la base de datos, porque viene del ACK.

---

## 4. Ingesta

**4.1. Recepción.** El Ingestor recibe el mensaje de `ppg/+/session`, extrae `device_key` y payload.

**4.2. Validación.** Verifica campos requeridos. Si falta algo: descarta, publica ACK de error, no toca la base de datos.

**4.3. Persistencia.** Una sola transacción:

- Busca `device_id` por `device_key`.
- Inserta la sesión. La base de datos genera el `session_code` automáticamente mediante `DEFAULT` sobre la columna.
- Copia masiva de las muestras a `raw_ppg`.
- COMMIT.

La tabla `sessions` es de solo inserción desde el punto de vista del Ingestor. El Ingestor no actualiza la sesión posteriormente ni mantiene estados de análisis.

La clave natural de `raw_ppg` es `(session_id, time)`, lo que permite reintentos idempotentes de las muestras sin duplicarlas.

Si el `INSERT` de la sesión colisiona con un `session_code` ya existente, el Ingestor reintenta la transacción completa. La unicidad la arbitra PostgreSQL, no el cliente.

Si la transacción falla por cualquier otro motivo, se revierte completa. Nunca quedan sesiones a medias ni muestras huérfanas.

**4.4. ACK.** Publica en `ppg/{device_key}/ack` con `session_id`, `session_code` y `status: received`. El código es lo que la Pi mostrará al usuario.

**4.5. Notificación.** Puede publicar en `analysis/{session_id}/request` únicamente como mecanismo de aviso para los analizadores. La notificación no forma parte de la persistencia ni crea trabajo en PostgreSQL. Un Analyzer puede usar MQTT, polling u otro mecanismo propio para descubrir sesiones disponibles.

---

## 5. Análisis

El Analyzer tiene una responsabilidad deliberadamente limitada: **obtener datos de entrada, procesarlos y generar resultados de su propio análisis**.

No administra dispositivos. No crea sesiones. No modifica `sessions`. No modifica `raw_ppg`. No genera `session_code`. No mantiene el estado global de la sesión. No depende de una tabla central `analysis_runs`. No ejecuta lógica de reconciliación sobre la base central.

El mecanismo para descubrir qué sesiones analizar pertenece al Analyzer y a su propio entorno. El contrato con la base central es simple: una sesión existente puede leerse; los datos crudos pueden leerse por `session_id`; los resultados generados se escriben en las tablas que correspondan al analizador.

### Fase 1: obtención

**5.1.** El Analyzer recibe o descubre un `session_id`.

**5.2.** Lee todas las muestras de `raw_ppg` para ese `session_id`, ordenadas por tiempo ascendente. DataFrame de pandas con aproximadamente 15,000 filas (5 minutos a 50 Hz).

El Analyzer no altera las muestras obtenidas. `raw_ppg` es entrada de solo lectura para el procesamiento.

### Fase 2: transformación

**5.3. Limpieza.** Filtro Butterworth paso banda de orden 4 entre 0.5 y 5 Hz sobre el canal IR. Eliminación de outliers por rango intercuartílico. Normalización a media cero y varianza unitaria.

**5.4. Control de calidad global.** Sobre la señal completa se calculan SQI, PI (ratio pulsátil/continuo del canal IR) y SNR en dB. Si alguno queda por debajo de umbral, el resultado del análisis se marca como `low_quality` y no se clasifica. No hay descarte por ventanas: la decisión es única para toda la sesión.

**5.5. Métricas temporales (HeartPy) sobre la señal completa de 5 min.** BPM, SDNN, RMSSD, pNN50, NN50, IBI medio. Cinco minutos es la ventana estándar para HRV de corto plazo: los umbrales publicados aplican sin ajustes.

**5.6. Métricas frecuenciales (Welch) sobre la serie RR interpolada.** LF (0.04–0.15 Hz), HF (0.15–0.4 Hz), potencia total. Derivadas: ratio LF/HF, LFnu, HFnu. La banda VLF se omite: con 5 minutos es matemáticamente calculable pero fisiológicamente sin sentido. Con 5 minutos, LF y HF son resolubles y el ratio LF/HF es interpretable.

**5.7. Frecuencia respiratoria.** Banda 0.15–0.5 Hz de la señal PPG (RIIV). FFT. Resultado en respiraciones por minuto.

### Fase 3: análisis

**5.8. Clasificación por reglas fisiológicas.** Cuatro reglas, cada una suma un punto:

- LF/HF > 2.0 → predominancia simpática.
- SDNN < 30 ms → variabilidad reducida.
- RMSSD < 20 ms → actividad parasimpática baja.
- pNN50 < 5% → baja variabilidad beat-to-beat.

Score = reglas cumplidas / 4. Etiqueta: 0–1 reglas → `no_stress`; 2 reglas → `no_stress` con score intermedio; 3–4 reglas → `stress`.

El score es un índice normalizado, no una probabilidad calibrada. Los umbrales son valores de referencia para adultos sanos en reposo, no una comparación contra población específica.

Si la sesión es `low_quality`: `label = 'low_quality'`, `score = NULL`. Las métricas que sí se pudieron calcular pueden formar parte del resultado del Analyzer; las que no, se omiten.

### Fase 4: generación de resultados

**5.9.** El Analyzer genera aproximadamente 14 resultados de métricas: temporales, frecuenciales, de calidad y respiratorios.

**5.10.** El Analyzer genera el resultado de clasificación con `analyzer = 'hrv'`, `label` y `score`.

**5.11.** Los resultados se escriben en las tablas pertenecientes al Analyzer. La escritura debe ser idempotente para una misma sesión y métrica.

**5.12.** El Analyzer puede publicar una notificación de finalización en MQTT, pero dicha notificación no modifica el estado de la sesión ni crea trabajo en la base central.

No existe una fase posterior en la que el Analyzer tenga que actualizar `sessions`, mantener una cola global o recalcular un estado central.

---

## 6. Responsabilidad del Analyzer

La frontera de responsabilidad del contenedor de análisis es explícita:

1. **Entrada:** identificar una sesión disponible.
2. **Lectura:** obtener de PostgreSQL los datos necesarios para esa sesión.
3. **Procesamiento:** transformar la señal y ejecutar su algoritmo.
4. **Salida:** generar y persistir los resultados propios del Analyzer.
5. **Notificación opcional:** informar que terminó.

Fuera de esa frontera, el Analyzer no realiza operaciones sobre el plano de ingesta.

En particular, el Analyzer **no**:

- crea dispositivos;
- crea sesiones;
- genera `session_code`;
- modifica metadatos de `sessions`;
- modifica o elimina muestras de `raw_ppg`;
- decide la retención de datos;
- administra una cola de trabajo central;
- cambia estados globales de sesiones;
- coordina otras familias de análisis;
- necesita conocer otros analizadores.

Añadir un nuevo Analyzer significa añadir su propio componente de procesamiento y sus propias tablas de resultados. El esquema central no necesita conocer su existencia.

---

## 7. Estado de los datos

El esquema central no mantiene un `sessions.status` derivado del análisis.

La tabla `sessions` representa únicamente que una captura fue persistida correctamente y contiene sus metadatos:

- `id`
- `device_id`
- `session_code`
- `started_at`
- `finished_at`
- `sample_count`
- `file_hash`
- `created_at`

La disponibilidad de resultados es una responsabilidad de la capa de consulta y del Analyzer correspondiente. No existe una transición central `uploaded → ready → error`.

Una sesión persistida puede existir aunque ningún Analyzer la haya procesado todavía.

---

## 8. Consulta del usuario final

**8.1. Acceso.** QR junto al nodo apunta a la web en el puerto 8000.

**8.2. Ingreso.** Formulario con campo de texto y botón "Consultar".

**8.3. Consulta.** Petición a la ruta pública de sesión por código.

**8.4. Verificación.** Se busca la sesión por `session_code`. La API consulta los resultados disponibles del Analyzer correspondiente. La existencia de una sesión no implica que sus resultados ya estén disponibles.

**8.5. Respuesta.** JSON con metadatos de la sesión y los resultados disponibles del analizador `hrv`.

**8.6. Presentación.**

Caso `stress` o `no_stress`: fecha, duración, etiqueta y score ("Sin estrés. Índice: 25/100"), BPM, SDNN, RMSSD, pNN50, ratio LF/HF, frecuencia respiratoria.

Caso `low_quality`: fecha, duración, mensaje "La señal fue de baja calidad. Intenta de nuevo asegurando buen contacto del dedo con el sensor." Sin métricas ni score, salvo que la interfaz administrativa necesite mostrar métricas parciales.

Si los resultados todavía no están disponibles, la API debe distinguir ese caso de una sesión inexistente. El texto exacto de la interfaz puede definirse en la implementación.

**8.7. Errores.** Si el código no existe, mensaje genérico "Código no encontrado". Si la sesión existe pero el resultado todavía no está disponible, se informa de que el análisis está pendiente.

---

## 9. Consulta del administrador

**9.1. Acceso.** Puerto 8000, ruta de administración. Sin autenticación en el prototipo.

**9.2. Tabla global.** Sesiones con sus metadatos y los resultados disponibles de los analizadores. No depende de una tabla central de trabajos.

**9.3. Filtros.** Por dispositivo, rango de fechas y resultado cuando ese resultado exista.

**9.4. Detalle.** Devuelve metadatos de la sesión y los resultados almacenados por los analizadores disponibles.

**9.5. Actualización.** Recarga manual. Sin WebSockets ni auto-refresh.

---

## 10. Operación continua

El ciclo de captura e ingesta se repite por cada captura.

**Múltiples dispositivos.** Cada Pi tiene su `device_key`. El Ingestor las procesa igual.

**Paralelismo.** Cada Analyzer puede escalar de acuerdo con su propio mecanismo de descubrimiento y procesamiento. La base central no coordina réplicas de analizadores.

**Reintentos de ingesta.** Si falla la persistencia, la transacción se revierte y la Pi vuelve a enviar el payload según su mecanismo de reintento.

**Retención.** TimescaleDB gestiona la retención de `raw_ppg`. El Analyzer no elimina muestras ni administra políticas de retención.

**Nuevos analizadores.** Se añade un contenedor con su propia lógica de descubrimiento, procesamiento y persistencia de resultados. No es necesario modificar el plano central de ingesta.

**Nuevos sensores.** Una nueva fuente puede utilizar su propio topic y sus propias tablas de entrada, manteniendo separada la responsabilidad del procesamiento.

---

## 11. Fin de la operación

Detener los contenedores con `docker compose down`. Los volúmenes de PostgreSQL y Mosquitto persisten.

Al reiniciar, el Ingestor vuelve a aceptar sesiones nuevas. Cada Analyzer retoma su propio mecanismo de descubrimiento y procesamiento.

Detener la Pi con `systemctl stop pulseppg`.

---

## 12. Resumen del recorrido

La Pi captura 5 minutos de PPG, comprime, hashea y publica por MQTT. No genera código.

El Ingestor valida el payload y persiste la sesión y sus muestras en una transacción. PostgreSQL genera el `session_code` y lo devuelve al Ingestor, que lo incluye en el ACK para la Pi.

La base de datos central conserva únicamente el plano de ingesta:

- `devices`: dispositivos registrados.
- `sessions`: metadatos de capturas persistidas.
- `raw_ppg`: señal cruda.

El Analyzer recibe o descubre un `session_id`, lee `raw_ppg`, procesa la señal y genera sus resultados. No administra el resto del sistema.

Para HRV:

- Obtención: lectura de `raw_ppg`.
- Transformación: filtrado, control de calidad global, métricas HRV temporales y frecuenciales, frecuencia respiratoria.
- Análisis: clasificación por reglas fisiológicas.
- Salida: resultados propios del Analyzer.

La API consulta la sesión y los resultados disponibles. La base central no necesita conocer el ciclo interno de ningún Analyzer.

---

## 13. Tecnologías

**Raspberry Pi:** Python 3.11+, smbus2, paho-mqtt, RPLCD, RPi.GPIO.

**Mosquitto:** imagen oficial `eclipse-mosquitto`, puerto 1883, persistencia en volumen.

**PostgreSQL + TimescaleDB:** imagen oficial, esquema en `init.sql`, retención configurada.

**Ingestor:** Python 3.11+, paho-mqtt, psycopg2. Sin framework web.

**Analyzer:** Python 3.11+, pandas, NumPy, SciPy, HeartPy, psycopg2, paho-mqtt. Sin framework web. El mecanismo de descubrimiento y procesamiento es responsabilidad del propio Analyzer.

**API:** Python 3.11+, FastAPI, Jinja2, psycopg2, Uvicorn, Bootstrap o Tailwind por CDN.

---

## 14. Convenciones de datos

**Topics MQTT:**

- `ppg/{device_key}/status`: online, offline.
- `ppg/{device_key}/session`: sesión completa.
- `ppg/{device_key}/ack`: confirmación al nodo.
- `analysis/{session_id}/request`: aviso de que existe una sesión disponible para analizar.
- `analysis/{session_id}/complete`: aviso de que un análisis terminó.

Los topics de análisis son mecanismos de comunicación, no una cola persistente central. La disponibilidad real de una sesión se determina leyendo PostgreSQL.

**Payload del ACK:**

- Éxito: `{ "session_id": 42, "session_code": "K7M2XP", "status": "received" }`
- Error interno: `{ "status": "error", "error": "internal" }`

**Código de sesión:** 6 caracteres Base32 (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`). Generado por la base de datos al insertar la sesión. 32^6 ≈ 1.07 × 10⁹ combinaciones. Sin vigencia: la sesión es consultable mientras exista.

**Métricas:** el Analyzer HRV produce temporales (`bpm`, `sdnn`, `rmssd`, `pnn50`, `nn50`, `ibi_mean`), frecuenciales (`lf`, `hf`, `lf_hf_ratio`, `lf_nu`, `hf_nu`, `total_power`), calidad (`sqi`, `perfusion_index`, `snr_db`) y respiratorias (`respiratory_rate`).

**Estados de análisis:** son responsabilidad del Analyzer. No existe un `analysis_runs` central definido en `init.sql`.

**Etiquetas (`stress_results.label`):** `stress`, `no_stress`, `low_quality`, cuando el Analyzer HRV utilice esas etiquetas.

---

## 15. Manejo de errores

**Pi:** señal inválida → descarta buffer y reinicia la captura completa. Sin ACK tras tres reintentos → guarda local y reintenta cada 5 minutos. El código se muestra solo cuando llega el ACK.

**Ingestor:** payload malformado → descarta, ACK de error, no toca la base de datos. Colisión de `session_code` → reintenta la transacción. Fallo en PostgreSQL → rollback completo, ACK de error. Fallo publicando el ACK → la Pi reintenta. Fallo publicando la notificación de análisis → no compromete la persistencia de la sesión.

**Analyzer:** fallo leyendo muestras → el Analyzer registra el error según su propio mecanismo de ejecución. Fallo de procesamiento → el resultado no se considera válido. El Analyzer puede reintentar según su propia política. Un fallo del Analyzer no modifica ni invalida la sesión ni la señal cruda.

**API:** fallo de PostgreSQL → 500 genérico. Código no encontrado → mensaje genérico de no encontrado. Sesión existente sin resultado disponible → respuesta diferenciada de análisis pendiente.

---

## 16. Retención de datos

Configurado en `init.sql` con políticas de retención de TimescaleDB:

- `raw_ppg`: 30 días. Voluminoso (unas 15,000 filas por sesión), leído por los analizadores.
- `sessions`: sin retención. Metadatos de las capturas.
- `devices`: sin retención. Catálogo de dispositivos.

Las tablas de resultados pertenecen a los analizadores y su retención se define de forma independiente. El Analyzer no ejecuta la retención de `raw_ppg`.

La retención de `raw_ppg` corre como job interno de TimescaleDB. No requiere intervención manual ni lógica adicional en los contenedores de análisis.

---

## 17. Glosario

ACK (Acknowledgement), API (Application Programming Interface), BPM (Beats Per Minute), ETL (Extract, Transform, Load), FFT (Fast Fourier Transform), GPIO (General Purpose Input/Output), HF (High Frequency, banda HRV 0.15–0.4 Hz), HRV (Heart Rate Variability), I2C (Inter-Integrated Circuit), IBI (Inter-Beat Interval), IQR (Interquartile Range), IR (Infrared), LF (Low Frequency, banda HRV 0.04–0.15 Hz), MQTT (Message Queuing Telemetry Transport), NN50 (intervalos NN que difieren más de 50 ms), pNN50 (porcentaje de esos intervalos), PI (Perfusion Index), PPG (Photoplethysmography), QR (Quick Response), RIIV (Respiratory-Induced Intensity Variation), RMSSD (Root Mean Square of Successive Differences), RR (intervalo entre picos consecutivos), SDNN (Standard Deviation of NN intervals), SHA-256 (Secure Hash Algorithm 256 bits), SNR (Signal-to-Noise Ratio), SQI (Signal Quality Index), UNS (Unified Namespace: aquí, la jerarquía de topics MQTT), VLF (Very Low Frequency, omitida), Welch (método de densidad espectral de potencia).
