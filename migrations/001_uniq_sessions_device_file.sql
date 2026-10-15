-- Aplica a una base ya creada el índice que init.sql añade para evitar
-- sesiones duplicadas cuando la Pi reenvía la misma captura.
-- init.sql solo corre con el volumen vacío; esto es para bases existentes.
--
-- Uso:
--   docker compose exec -T db psql -U pulseppg -d pulseppg \
--       -v ON_ERROR_STOP=1 < migrations/001_uniq_sessions_device_file.sql
--
-- Si falla con "could not create unique index", ya hay sesiones repetidas;
-- se listan con:
--   SELECT device_id, file_hash, array_agg(id ORDER BY id)
--   FROM sessions GROUP BY 1, 2 HAVING count(*) > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_sessions_device_file
    ON sessions (device_id, file_hash);
