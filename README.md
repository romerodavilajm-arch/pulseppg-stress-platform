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

Por ahora `compose.yaml` solo levanta PostgreSQL 16 + TimescaleDB con el esquema de `init.sql`.

```bash
cp .env.example .env            # opcional: credenciales y puerto
docker compose up -d --wait db  # espera a que init.sql termine y la BD esté sana
docker compose exec -T db psql -U pulseppg -d pulseppg -v ON_ERROR_STOP=1 < tests/db/smoke_test.sql
```

La prueba termina con `SMOKE TEST OK` y no deja datos (corre en una transacción con `ROLLBACK`). `init.sql` solo se ejecuta con el volumen vacío; para recrear la base desde cero: `docker compose down -v`.

## Documentación

Consulta [`docs/flujo.md`](docs/flujo.md) para el recorrido completo de los datos, las decisiones de diseño y el detalle del pipeline ETL.

## Licencia

Este proyecto se distribuye bajo licencia MIT. Consulta el archivo [LICENSE](LICENSE) para más detalles.

## Autor

Desarrollado por un estudiante de Ingeniería en Sistemas Computacionales del Instituto Tecnológico de Querétaro, para el 1er Congreso Internacional de Innovación Tecnológica "Código sin fronteras".
