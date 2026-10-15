-- ============================================================
-- PulsePPG Stress Platform — Esquema propio del Analyzer HRV
--
-- Lo crea el propio Analyzer al arrancar (idempotente). El esquema
-- central (init.sql) no lo conoce: el Analyzer solo lee sessions y
-- raw_ppg y escribe aquí sus resultados (docs/flujo.md §5-6).
-- ============================================================

CREATE SCHEMA IF NOT EXISTS hrv;

-- Métricas: una fila por sesión y métrica (docs/flujo.md §14).
-- Clave (session_id, metric): reanalizar una sesión no duplica nada.
CREATE TABLE IF NOT EXISTS hrv.metrics (
    session_id  INTEGER          NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    metric      TEXT             NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    computed_at TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, metric)
);

-- Clasificación de estrés por reglas fisiológicas (docs/flujo.md §5.8).
-- score = reglas cumplidas / 4; NULL si la señal fue de baja calidad.
CREATE TABLE IF NOT EXISTS hrv.stress_results (
    session_id  INTEGER      PRIMARY KEY REFERENCES public.sessions(id) ON DELETE CASCADE,
    analyzer    TEXT         NOT NULL DEFAULT 'hrv' CHECK (analyzer = 'hrv'),
    label       TEXT         NOT NULL CHECK (label IN ('stress', 'no_stress', 'low_quality')),
    score       NUMERIC(3,2) CHECK (score BETWEEN 0 AND 1),
    rules_met   SMALLINT     CHECK (rules_met BETWEEN 0 AND 4),
    computed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_stress_results_low_quality
        CHECK ((label = 'low_quality') = (score IS NULL))
);

-- Mecanismo de descubrimiento propio: sesiones que fallaron al analizarse.
-- Una sesión sin fila en stress_results y con menos de max_attempts
-- intentos fallidos sigue pendiente para este Analyzer.
CREATE TABLE IF NOT EXISTS hrv.failures (
    session_id   INTEGER     PRIMARY KEY REFERENCES public.sessions(id) ON DELETE CASCADE,
    attempts     INTEGER     NOT NULL DEFAULT 1,
    permanent    BOOLEAN     NOT NULL DEFAULT FALSE,
    error        TEXT        NOT NULL,
    last_attempt TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
