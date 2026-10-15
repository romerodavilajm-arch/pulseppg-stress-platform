-- ============================================================
-- PulsePPG Stress Platform — Prueba de humo del esquema central
--
-- Verifica que init.sql dejó la base en el estado esperado y que
-- las consultas de referencia del Ingestor y del Analyzer
-- funcionan. Todo corre dentro de una transacción que termina
-- en ROLLBACK: no deja datos en la base.
--
-- Uso:
--   docker compose exec -T db psql -U pulseppg -d pulseppg \
--       -v ON_ERROR_STOP=1 < tests/db/smoke_test.sql
--
-- Si alguna comprobación falla, psql se detiene con un ERROR
-- que indica cuál. Si todo pasa, la última línea es
-- "SMOKE TEST OK".
-- ============================================================

\set QUIET on
\pset footer off

BEGIN;

-- ------------------------------------------------------------
-- 1. Estructura: extensión, tablas, hypertable y retención
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE EXCEPTION 'FALLO: la extensión timescaledb no está instalada';
    END IF;

    IF to_regclass('public.devices') IS NULL
       OR to_regclass('public.sessions') IS NULL
       OR to_regclass('public.raw_ppg') IS NULL THEN
        RAISE EXCEPTION 'FALLO: faltan tablas del esquema central';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_name = 'raw_ppg') THEN
        RAISE EXCEPTION 'FALLO: raw_ppg no es una hypertable';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs
                   WHERE hypertable_name = 'raw_ppg'
                     AND proc_name = 'policy_retention'
                     AND config->>'drop_after' = '30 days') THEN
        RAISE EXCEPTION 'FALLO: no existe la política de retención de 30 días en raw_ppg';
    END IF;

    IF to_regclass('public.uniq_raw_ppg_session_time') IS NULL THEN
        RAISE EXCEPTION 'FALLO: falta el índice único (session_id, time) en raw_ppg';
    END IF;

    RAISE NOTICE 'OK 1/6: extensión, tablas, hypertable, índice único y retención';
END $$;

-- ------------------------------------------------------------
-- 2. Ingesta: dispositivo + sesión con código generado por la BD
-- ------------------------------------------------------------
INSERT INTO devices (device_key) VALUES ('smoke-test-pi');

CREATE TEMP TABLE t_session ON COMMIT DROP AS
WITH s AS (
    INSERT INTO sessions (device_id, started_at, finished_at, sample_count, file_hash)
    SELECT id,
           '2026-01-01 10:00:00+00',
           '2026-01-01 10:05:00+00',
           15000,
           repeat('a', 64)
    FROM devices WHERE device_key = 'smoke-test-pi'
    RETURNING id, session_code
)
SELECT * FROM s;

DO $$
DECLARE
    code TEXT;
BEGIN
    SELECT session_code INTO code FROM t_session;
    IF code IS NULL OR code !~ '^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$' THEN
        RAISE EXCEPTION 'FALLO: session_code inválido: %', code;
    END IF;
    RAISE NOTICE 'OK 2/6: sesión creada con session_code=%', code;
END $$;

-- ------------------------------------------------------------
-- 3. Muestras: 5 minutos a 50 Hz = 15 000 filas
-- ------------------------------------------------------------
INSERT INTO raw_ppg (time, session_id, ir, red)
SELECT '2026-01-01 10:00:00+00'::timestamptz + (n * INTERVAL '20 milliseconds'),
       (SELECT id FROM t_session),
       100000 + (1000 * sin(n / 10.0))::int,
       80000  + (800  * sin(n / 10.0))::int
FROM generate_series(0, 14999) AS n;

DO $$
DECLARE
    n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM raw_ppg WHERE session_id = (SELECT id FROM t_session);
    IF n <> 15000 THEN
        RAISE EXCEPTION 'FALLO: se esperaban 15000 muestras, hay %', n;
    END IF;
    RAISE NOTICE 'OK 3/6: 15000 muestras insertadas en raw_ppg';
END $$;

-- ------------------------------------------------------------
-- 4. Idempotencia: reintento del Ingestor con ON CONFLICT
-- ------------------------------------------------------------
DO $$
DECLARE
    inserted BIGINT;
BEGIN
    WITH ins AS (
        INSERT INTO raw_ppg (time, session_id, ir, red)
        SELECT '2026-01-01 10:00:00+00'::timestamptz + (n * INTERVAL '20 milliseconds'),
               (SELECT id FROM t_session), 0, 0
        FROM generate_series(0, 99) AS n
        ON CONFLICT (session_id, time) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO inserted FROM ins;

    IF inserted <> 0 THEN
        RAISE EXCEPTION 'FALLO: ON CONFLICT insertó % duplicados', inserted;
    END IF;
    RAISE NOTICE 'OK 4/6: reintento idempotente, 0 duplicados';
END $$;

-- ------------------------------------------------------------
-- 5. Restricciones: datos inválidos deben rechazarse
-- ------------------------------------------------------------
DO $$
DECLARE
    dev INT;
BEGIN
    SELECT id INTO dev FROM devices WHERE device_key = 'smoke-test-pi';

    BEGIN
        INSERT INTO sessions (device_id, started_at, finished_at, sample_count, file_hash)
        VALUES (dev, '2026-01-01 10:05:00+00', '2026-01-01 10:00:00+00', 1, repeat('a', 64));
        RAISE EXCEPTION 'FALLO: se aceptó finished_at <= started_at';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO sessions (device_id, started_at, finished_at, sample_count, file_hash)
        VALUES (dev, '2026-01-01 10:00:00+00', '2026-01-01 10:05:00+00', 1, repeat('Z', 64));
        RAISE EXCEPTION 'FALLO: se aceptó un file_hash que no es SHA-256 hex';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO sessions (device_id, session_code, started_at, finished_at, sample_count, file_hash)
        VALUES (dev, 'ABC10I', '2026-01-01 10:00:00+00', '2026-01-01 10:05:00+00', 1, repeat('a', 64));
        RAISE EXCEPTION 'FALLO: se aceptó un session_code con caracteres ambiguos';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO sessions (device_id, session_code, started_at, finished_at, sample_count, file_hash)
        SELECT dev, session_code, '2026-01-01 11:00:00+00', '2026-01-01 11:05:00+00', 1, repeat('b', 64)
        FROM t_session;
        RAISE EXCEPTION 'FALLO: se aceptó un session_code duplicado';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO raw_ppg (time, session_id, ir, red)
        VALUES ('2026-01-01 10:00:00+00', -1, 0, 0);
        RAISE EXCEPTION 'FALLO: se aceptó una muestra de una sesión inexistente';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;

    RAISE NOTICE 'OK 5/6: restricciones de tiempo, hash, código, unicidad y FK';
END $$;

-- ------------------------------------------------------------
-- 6. Consultas de referencia (API y Analyzer) y borrado en cascada
-- ------------------------------------------------------------
DO $$
DECLARE
    sid      INT;
    found_id INT;
    first_t  TIMESTAMPTZ;
    last_t   TIMESTAMPTZ;
    n        BIGINT;
BEGIN
    SELECT id INTO sid FROM t_session;

    -- API: consulta de sesión por código
    SELECT s.id INTO found_id FROM sessions s
    WHERE s.session_code = (SELECT session_code FROM t_session);
    IF found_id IS DISTINCT FROM sid THEN
        RAISE EXCEPTION 'FALLO: la consulta por session_code no devuelve la sesión';
    END IF;

    -- Analyzer: lectura ordenada de muestras
    SELECT min(time), max(time), count(*) INTO first_t, last_t, n
    FROM (SELECT time FROM raw_ppg WHERE session_id = sid ORDER BY time ASC) q;
    IF n <> 15000 OR last_t - first_t <> INTERVAL '299.98 seconds' THEN
        RAISE EXCEPTION 'FALLO: lectura del Analyzer inesperada (n=%, rango=%)', n, last_t - first_t;
    END IF;

    -- ON DELETE CASCADE desde sessions hacia la hypertable
    DELETE FROM sessions WHERE id = sid;
    SELECT count(*) INTO n FROM raw_ppg WHERE session_id = sid;
    IF n <> 0 THEN
        RAISE EXCEPTION 'FALLO: ON DELETE CASCADE dejó % muestras huérfanas', n;
    END IF;

    RAISE NOTICE 'OK 6/6: consulta por código, lectura del Analyzer y borrado en cascada';
END $$;

ROLLBACK;

\echo 'SMOKE TEST OK'
