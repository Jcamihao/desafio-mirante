-- Idempotente: serve ao docker-entrypoint-initdb.d (1o start) e ao migrador do app (rastreado em
-- schema_migrations). Rodar duas vezes nao falha.
DO $$
BEGIN
    CREATE TYPE modernization_status AS ENUM ('SUCCESS', 'PARTIAL', 'FAILURE');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS modernization_history (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_code    TEXT                 NOT NULL,
    generated_code TEXT,                          -- NULL quando nao chegou a gerar
    report         JSONB                NOT NULL DEFAULT '{}'::jsonb,
    status         modernization_status NOT NULL,
    created_at     TIMESTAMPTZ          NOT NULL DEFAULT now(),
    -- colunas de consulta (derivadas do relatorio, evitam varrer JSONB em listagens/metricas)
    procedure_name TEXT,
    dialect        TEXT                 NOT NULL DEFAULT 'postgres',
    duration_ms    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_modernization_history_created_at
    ON modernization_history (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_modernization_history_status
    ON modernization_history (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_modernization_history_procedure
    ON modernization_history (procedure_name);
-- Permite consultar o relatorio (ex.: report @> '{"validation": {"status": "passed"}}')
CREATE INDEX IF NOT EXISTS idx_modernization_history_report
    ON modernization_history USING GIN (report jsonb_path_ops);
