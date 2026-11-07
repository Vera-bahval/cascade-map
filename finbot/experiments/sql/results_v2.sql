-- Additive extension: requires CREATE in public and REFERENCES on the base tables.
-- Existing tables and views may belong to a different archive writer.
CREATE TABLE IF NOT EXISTS public.cm_series_configs (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL,
    manifest jsonb NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.cm_series (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    status text NOT NULL,
    manifest jsonb NOT NULL,
    plan jsonb NOT NULL,
    metadata jsonb NOT NULL,
    source_updated_at timestamptz,
    archived_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.cm_series_runs (
    run_id uuid PRIMARY KEY REFERENCES public.cm_runs(id) ON DELETE CASCADE,
    series_id uuid NOT NULL REFERENCES public.cm_series(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public.cm_case_observations (
    case_id uuid PRIMARY KEY REFERENCES public.cm_cases(id) ON DELETE CASCADE,
    observations jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS public.cm_agent_iterations (
    id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES public.cm_cases(id) ON DELETE CASCADE,
    agent_run_id uuid REFERENCES public.cm_agent_runs(id),
    source_agent_run_id text NOT NULL,
    agent_name text NOT NULL,
    iteration_index integer NOT NULL CHECK(iteration_index >= 1),
    started_at timestamptz,
    finished_at timestamptz,
    execution_status text NOT NULL,
    vanilla_confidence double precision CHECK(vanilla_confidence BETWEEN 0 AND 1),
    lexical_confidence jsonb,
    trace jsonb NOT NULL,
    UNIQUE(case_id, source_agent_run_id, iteration_index)
);

CREATE TABLE IF NOT EXISTS public.cm_iteration_evaluations (
    iteration_id uuid PRIMARY KEY REFERENCES public.cm_agent_iterations(id) ON DELETE CASCADE,
    status text NOT NULL,
    confidence double precision CHECK(confidence BETWEEN 0 AND 1),
    error_present boolean,
    assessment jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS cm_series_runs_series_idx ON public.cm_series_runs(series_id);
CREATE INDEX IF NOT EXISTS cm_iterations_agent_idx ON public.cm_agent_iterations(agent_run_id);
