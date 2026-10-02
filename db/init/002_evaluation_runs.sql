-- Resultados de avaliacao do pipeline (metricas agregadas + resultado por caso). Idempotente.
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id         UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    model      TEXT        NOT NULL DEFAULT '',
    metrics    JSONB       NOT NULL,
    results    JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_created_at ON evaluation_runs (created_at DESC);
