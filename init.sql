-- ============================================================
-- PulsePPG Stress Platform — Esquema central
-- PostgreSQL 16 + TimescaleDB
-- Se ejecuta una sola vez al crear el contenedor.
-- ============================================================
--
-- Este esquema describe el plano de ingesta: dispositivos,
-- sesiones y muestras crudas. Es la única fuente de verdad
-- sobre qué sesiones existen.
--
-- Los analizadores no se declaran aquí. Cada uno vive en su
-- propio esquema, con sus propias tablas y su propia lógica
-- de descubrimiento de trabajo. Este archivo no los conoce.
--
-- CLAVES NATURALES PARA ON CONFLICT:
--   sessions:  (session_code)  -- generado por DEFAULT
--   sessions:  (device_id, file_hash)  -- reenvíos de la Pi
--   raw_ppg:   (session_id, time)
--
-- NOTA SOBRE TIMESCALEDB:
-- Los índices únicos en hypertables DEBEN incluir la columna
-- de particionamiento (time). TimescaleDB descarta
-- silenciosamente cualquier UNIQUE inline que no la incluya.
--
-- NOTA SOBRE EL CÓDIGO DE SESIÓN:
-- El código lo genera la base de datos mediante la función
-- generate_session_code() aplicada como DEFAULT sobre la
-- columna. La unicidad la arbitra la restricción UNIQUE, no
-- el cliente. Si el INSERT colisiona, el Ingestor reintenta
-- la transacción. Con 32^6 ≈ 1.07e9 combinaciones y unos
-- pocos miles de sesiones, la probabilidad de colisión por
-- inserción es del orden de 1e-6.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;


-- ============================================================
-- 0. Función generadora de códigos de sesión
--    Alfabeto Base32 sin caracteres ambiguos (sin 0, 1, I, O).
--    Devuelve 6 caracteres. Volátil por el uso de random().
-- ============================================================

CREATE OR REPLACE FUNCTION generate_session_code()
RETURNS CHAR(6) AS $$
DECLARE
    alphabet CONSTANT TEXT := 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    result   TEXT := '';
    i        INT;
BEGIN
    FOR i IN 1..6 LOOP
        result := result
                  || substr(alphabet,
                            1 + floor(random() * 32)::int,
                            1);
    END LOOP;
    RETURN result::CHAR(6);
END;
$$ LANGUAGE plpgsql VOLATILE;

COMMENT ON FUNCTION generate_session_code() IS
    'Genera un código Base32 de 6 caracteres. Usado como DEFAULT en '
    'sessions.session_code. La unicidad la garantiza la restricción UNIQUE; '
    'el Ingestor reintenta la transacción si hubiera colisión.';


-- ============================================================
-- 1. devices
--    Catálogo de nodos registrados. El dispositivo se identifica
--    por su device_key. No hay prefijo: el código de sesión se
--    genera en la base de datos y no depende del dispositivo.
-- ============================================================

CREATE TABLE IF NOT EXISTS devices (
    id         SERIAL PRIMARY KEY,
    device_key TEXT        NOT NULL UNIQUE,
    last_seen  TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE devices IS
    'Catálogo de nodos autorizados. El Ingestor resuelve device_id a partir '
    'del device_key que viene en el topic MQTT.';


-- ============================================================
-- 2. sessions
--    Metadatos de cada captura. La base genera el session_code
--    al insertar. No hay columnas de estado de análisis: el
--    ciclo de vida del análisis vive en cada analizador.
--
--    La tabla es de solo inserción desde el punto de vista del
--    Ingestor. Nadie la modifica después del INSERT.
-- ============================================================

CREATE TABLE IF NOT EXISTS sessions (
    id           SERIAL PRIMARY KEY,
    device_id    INTEGER     NOT NULL REFERENCES devices(id),
    session_code CHAR(6)     NOT NULL UNIQUE
                 DEFAULT generate_session_code()
                 CHECK (session_code ~ '^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$'),
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ NOT NULL,
    sample_count INTEGER     NOT NULL CHECK (sample_count >= 0),
    file_hash    CHAR(64)    NOT NULL
                 CHECK (file_hash ~ '^[0-9a-f]{64}$'),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_sessions_time_order
        CHECK (finished_at > started_at)
);

CREATE INDEX IF NOT EXISTS idx_sessions_device  ON sessions (device_id);

-- Una misma captura (mismo archivo) de un mismo dispositivo solo se
-- guarda una vez. Si la Pi reenvía la sesión porque no le llegó el
-- ACK, el Ingestor devuelve el session_code que ya existe.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_sessions_device_file
    ON sessions (device_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions (created_at DESC);

COMMENT ON COLUMN sessions.session_code IS
    'Código Base32 de 6 caracteres generado por generate_session_code() al '
    'insertar la fila. Es lo que el usuario final usa para consultar. '
    'Sin vigencia: la sesión es consultable mientras exista.';

COMMENT ON CONSTRAINT chk_sessions_time_order ON sessions IS
    'La sesión debe terminar después de empezar. Un finished_at anterior o igual '
    'a started_at indica un error de reloj en la Pi o un payload malformado.';

COMMENT ON TABLE sessions IS
    'Metadatos de cada captura. Solo inserción desde el Ingestor. El código '
    'de sesión se genera aquí y se devuelve en el ACK al nodo.';


-- ============================================================
-- 3. raw_ppg (hypertable)
--    Muestras crudas del MAX30102. Retención de 30 días.
--    Es la única tabla que crece con el volumen de la señal.
-- ============================================================

CREATE TABLE IF NOT EXISTS raw_ppg (
    time       TIMESTAMPTZ NOT NULL,
    session_id INTEGER     NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ir         INTEGER     NOT NULL,
    red        INTEGER     NOT NULL
);

SELECT create_hypertable('raw_ppg', 'time', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_raw_ppg_session_time
    ON raw_ppg (session_id, time);

CREATE INDEX IF NOT EXISTS idx_raw_ppg_session
    ON raw_ppg (session_id, time DESC);

COMMENT ON INDEX uniq_raw_ppg_session_time IS
    'Clave natural de raw_ppg. Habilita ON CONFLICT para reintentos idempotentes '
    'del Ingestor sin duplicar muestras.';

COMMENT ON TABLE raw_ppg IS
    'Muestras crudas del sensor. Aproximadamente 15,000 filas por sesión de '
    '5 minutos a 50 Hz. Retención de 30 días vía TimescaleDB.';


-- ============================================================
-- 4. Política de retención
-- ============================================================

SELECT add_retention_policy('raw_ppg', INTERVAL '30 days', if_not_exists => TRUE);


-- ============================================================
-- 5. Ejemplos de consultas de referencia
--     (comentadas, solo documentación)
-- ============================================================
--
-- INSERCIÓN DE UNA SESIÓN DELEGANDO EL CÓDIGO A LA BASE:
--
--   INSERT INTO sessions
--       (device_id, started_at, finished_at, sample_count, file_hash)
--   VALUES ($1, $2, $3, $4, $5)
--   RETURNING id, session_code;
--
--   Si la inserción viola uniq de session_code, el Ingestor
--   reintenta la transacción completa. En la práctica nunca
--   se llega al segundo intento.
--
-- COPIA MASIVA DE MUESTRAS (misma transacción que la sesión):
--
--   COPY raw_ppg (time, session_id, ir, red) FROM STDIN;
--
-- CONSULTA DE UNA SESIÓN POR CÓDIGO:
--
--   SELECT id, session_code, started_at, finished_at, sample_count
--   FROM sessions
--   WHERE session_code = $1;
--
-- LECTURA DE MUESTRAS PARA UN ANALIZADOR:
--
--   SELECT time, ir, red
--   FROM raw_ppg
--   WHERE session_id = $1
--   ORDER BY time ASC;
