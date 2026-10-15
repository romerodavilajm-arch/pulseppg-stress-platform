-- Comprueba en la BD la sesión que devolvió el ACK del Ingestor.
-- Uso: psql ... -v code=XXXXXX < tests/ingestor/check_session.sql
\set QUIET on
\pset footer off

SET pulseppg.code = :'code';

SELECT s.id, s.session_code, d.device_key, s.sample_count,
       (SELECT count(*) FROM raw_ppg r WHERE r.session_id = s.id) AS raw_rows,
       s.finished_at - s.started_at AS duration,
       d.last_seen IS NOT NULL AS device_seen
FROM sessions s JOIN devices d ON d.id = s.device_id
WHERE s.session_code = :'code';

DO $$
DECLARE
    expected INT;
    actual   BIGINT;
BEGIN
    SELECT s.sample_count, (SELECT count(*) FROM raw_ppg r WHERE r.session_id = s.id)
      INTO expected, actual
      FROM sessions s WHERE s.session_code = current_setting('pulseppg.code');
    IF expected IS NULL THEN
        RAISE EXCEPTION 'FALLO: la sesión no existe en la BD';
    END IF;
    IF actual <> expected THEN
        RAISE EXCEPTION 'FALLO: sample_count=% pero raw_ppg tiene % filas', expected, actual;
    END IF;
END $$;

\echo 'INGESTOR CHECK OK'
