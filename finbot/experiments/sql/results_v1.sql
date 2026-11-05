CREATE TABLE IF NOT EXISTS public.cm_runs (
    id uuid PRIMARY KEY,
    source_kind text NOT NULL,
    source_run_id text NOT NULL,
    submitted_by text,
    model text,
    protocol text,
    status text NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    is_demo boolean NOT NULL DEFAULT false,
    config jsonb NOT NULL,
    metrics jsonb NOT NULL,
    metadata jsonb NOT NULL,
    source_updated_at timestamptz,
    archived_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_kind, source_run_id)
);

CREATE TABLE IF NOT EXISTS public.cm_cases (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES public.cm_runs(id) ON DELETE CASCADE,
    source_case_id text NOT NULL,
    scenario_id text NOT NULL,
    repeat_index integer NOT NULL CHECK (repeat_index >= 1),
    execution_status text NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    duration_ms double precision CHECK (duration_ms >= 0),
    expected_behavior text,
    report jsonb NOT NULL,
    result jsonb NOT NULL,
    metrics jsonb NOT NULL,
    human_review jsonb NOT NULL,
    scenario_snapshot jsonb NOT NULL,
    cascade_location jsonb NOT NULL,
    provenance jsonb NOT NULL,
    UNIQUE (run_id, source_case_id)
);

CREATE TABLE IF NOT EXISTS public.cm_agent_runs (
    id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES public.cm_cases(id) ON DELETE CASCADE,
    parent_agent_run_id uuid REFERENCES public.cm_agent_runs(id) DEFERRABLE INITIALLY DEFERRED,
    source_agent_run_id text,
    agent_name text NOT NULL,
    step_order integer,
    execution_status text,
    started_at timestamptz,
    finished_at timestamptz,
    duration_ms double precision CHECK (duration_ms >= 0),
    summary text,
    result jsonb NOT NULL,
    input jsonb NOT NULL,
    heuristic_confidence double precision CHECK (heuristic_confidence BETWEEN 0 AND 1),
    model_confidence double precision CHECK (model_confidence BETWEEN 0 AND 1),
    metrics jsonb NOT NULL,
    provenance jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS public.cm_llm_calls (
    id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES public.cm_cases(id) ON DELETE CASCADE,
    agent_run_id uuid REFERENCES public.cm_agent_runs(id),
    source_interaction_id text,
    call_index integer NOT NULL,
    agent_name text,
    occurred_at timestamptz,
    success boolean,
    duration_ms double precision CHECK (duration_ms >= 0),
    model_requested text,
    model_returned text,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    cached_tokens bigint CHECK (cached_tokens >= 0),
    reasoning_tokens bigint CHECK (reasoning_tokens >= 0),
    estimated_input_tokens bigint CHECK (estimated_input_tokens >= 0),
    estimated_output_tokens bigint CHECK (estimated_output_tokens >= 0),
    effective_params jsonb NOT NULL,
    request jsonb NOT NULL,
    response jsonb NOT NULL,
    error jsonb NOT NULL,
    provenance jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS public.cm_events (
    id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES public.cm_cases(id) ON DELETE CASCADE,
    agent_run_id uuid REFERENCES public.cm_agent_runs(id),
    source_seq text NOT NULL,
    occurred_at timestamptz,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (case_id, source_seq)
);

ALTER TABLE public.cm_llm_calls ADD COLUMN IF NOT EXISTS cost numeric CHECK (cost >= 0);
ALTER TABLE public.cm_llm_calls ADD COLUMN IF NOT EXISTS currency text;
ALTER TABLE public.cm_llm_calls ADD COLUMN IF NOT EXISTS cost_basis text;

CREATE INDEX IF NOT EXISTS cm_cases_run_idx ON public.cm_cases(run_id);
CREATE INDEX IF NOT EXISTS cm_agent_runs_case_idx ON public.cm_agent_runs(case_id);
CREATE INDEX IF NOT EXISTS cm_llm_calls_agent_idx ON public.cm_llm_calls(agent_run_id);
CREATE INDEX IF NOT EXISTS cm_llm_calls_case_idx ON public.cm_llm_calls(case_id);
CREATE INDEX IF NOT EXISTS cm_events_case_idx ON public.cm_events(case_id);
CREATE INDEX IF NOT EXISTS cm_runs_model_idx ON public.cm_runs(model, started_at);

CREATE OR REPLACE VIEW public.cm_request_results AS
SELECT r.id AS run_id, c.id AS case_id, l.id AS request_id,
       l.agent_run_id, l.agent_name, l.model_requested AS model, l.model_returned,
       l.occurred_at AS requested_at, l.cost, l.currency, l.cost_basis,
       r.config AS run_config, l.effective_params AS request_config,
       l.provenance -> 'pricing' AS pricing,
       l.input_tokens, l.output_tokens, l.cached_tokens, l.success
FROM public.cm_llm_calls l JOIN public.cm_cases c ON c.id=l.case_id
JOIN public.cm_runs r ON r.id=c.run_id;

CREATE OR REPLACE VIEW public.cm_scenario_results AS
SELECT r.id AS run_id, c.id AS case_id, r.submitted_by, r.model, r.protocol,
       r.config AS parameters, r.source_kind, r.source_run_id, r.is_demo,
       c.scenario_id, c.repeat_index, c.started_at, c.finished_at,
       c.execution_status, c.duration_ms / 1000 AS duration_seconds,
       c.expected_behavior,
       c.report #>> '{outcome,status}' AS outcome_status,
       c.report #>> '{outcome,summary}' AS actual_outcome,
       c.report #>> '{outcome,basis}' AS outcome_basis,
       c.report #>> '{detector,type}' AS detector_type,
       c.report #>> '{detector,label}' AS detector_label,
       c.report #>> '{detector,explanation}' AS detector_explanation,
       c.report #>> '{cascade,status}' AS cascade_status,
       c.report #>> '{cascade,label}' AS cascade_label,
       c.report #>> '{cascade,summary}' AS cascade_explanation,
       c.cascade_location ->> 'first_signal_agent' AS first_signal_agent,
       c.cascade_location ->> 'first_signal_step' AS first_signal_step,
       c.cascade_location ->> 'confirmed_origin_agent' AS confirmed_origin_agent,
       c.cascade_location AS cascade_location,
       c.human_review, c.report -> 'timeline' AS agent_timeline,
       c.report -> 'observations' AS evidence,
       c.report -> 'unresolved' AS unresolved,
       c.metrics, c.provenance,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' THEN t.llm_calls END AS llm_calls,
       t.llm_calls AS observed_llm_calls,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' AND t.success_known=t.llm_calls THEN t.failed_calls END AS failed_calls,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' AND t.duration_known=t.llm_calls THEN t.llm_duration_seconds END AS llm_duration_seconds,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' AND t.llm_calls > 0 AND t.input_known = t.llm_calls THEN t.input_sum END AS input_tokens,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' AND t.llm_calls > 0 AND t.output_known = t.llm_calls THEN t.output_sum END AS output_tokens,
       t.input_sum AS known_input_tokens, t.output_sum AS known_output_tokens,
       t.missing_usage_calls,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' AND t.estimated_input_known=t.llm_calls THEN t.estimated_input_tokens END AS estimated_input_tokens,
       CASE WHEN c.provenance->>'call_coverage_complete'='true' AND t.estimated_output_known=t.llm_calls THEN t.estimated_output_tokens END AS estimated_output_tokens,
       t.llm_duration_seconds AS known_llm_duration_seconds,
       t.failed_calls AS observed_failed_calls,
       t.estimated_input_tokens AS known_estimated_input_tokens,
       t.estimated_output_tokens AS known_estimated_output_tokens,
       c.cascade_location ->> 'detected_origin_agent' AS detected_origin_agent,
       c.cascade_location ->> 'detected_origin_step' AS detected_origin_step,
       c.cascade_location ->> 'detected_origin_event_seq' AS detected_origin_event_seq,
       c.cascade_location ->> 'detected_origin_basis' AS detected_origin_basis,
       c.cascade_location -> 'detected_origin_evidence' AS detected_origin_evidence,
       c.cascade_location -> 'propagation' AS error_propagation,
       c.report #>> '{detector,version}' AS detector_version
FROM public.cm_cases c JOIN public.cm_runs r ON r.id=c.run_id
LEFT JOIN LATERAL (
    SELECT count(*) AS llm_calls,
           count(*) FILTER (WHERE success=false) AS failed_calls,
           sum(duration_ms)/1000 AS llm_duration_seconds,
           count(duration_ms) AS duration_known, count(success) AS success_known,
           count(estimated_input_tokens) AS estimated_input_known,
           count(estimated_output_tokens) AS estimated_output_known,
           count(input_tokens) AS input_known, count(output_tokens) AS output_known,
           sum(input_tokens) AS input_sum, sum(output_tokens) AS output_sum,
           count(*) FILTER (WHERE input_tokens IS NULL OR output_tokens IS NULL) AS missing_usage_calls,
           sum(estimated_input_tokens) AS estimated_input_tokens,
           sum(estimated_output_tokens) AS estimated_output_tokens
    FROM public.cm_llm_calls l WHERE l.case_id=c.id
) t ON true;

CREATE OR REPLACE VIEW public.cm_agent_results AS
SELECT r.id AS run_id, c.id AS case_id, a.id AS agent_run_id,
       a.parent_agent_run_id, r.submitted_by, r.model, r.config AS parameters,
       r.source_kind, r.is_demo, c.scenario_id, c.repeat_index,
       a.step_order, a.agent_name, a.execution_status, a.started_at, a.finished_at,
       a.duration_ms/1000 AS duration_seconds, a.summary, a.result,
       a.heuristic_confidence, a.model_confidence, a.provenance,
       c.expected_behavior, c.report #>> '{outcome,summary}' AS actual_outcome,
       c.report #>> '{detector,type}' AS detector_type,
       c.report #>> '{cascade,status}' AS cascade_status,
       c.cascade_location,
       CASE WHEN a.metrics->>'call_coverage'='complete' THEN t.llm_calls END AS llm_calls,
       t.llm_calls AS observed_llm_calls,
       CASE WHEN a.metrics->>'call_coverage'='complete' AND t.success_known=t.llm_calls THEN t.failed_calls END AS failed_calls,
       CASE WHEN a.metrics->>'call_coverage'='complete' AND t.duration_known=t.llm_calls THEN t.llm_duration_seconds END AS llm_duration_seconds,
       CASE WHEN a.metrics->>'call_coverage'='complete' AND t.llm_calls > 0 AND t.input_known=t.llm_calls THEN t.input_sum END AS input_tokens,
       CASE WHEN a.metrics->>'call_coverage'='complete' AND t.llm_calls > 0 AND t.output_known=t.llm_calls THEN t.output_sum END AS output_tokens,
       t.input_sum AS known_input_tokens, t.output_sum AS known_output_tokens,
       t.missing_usage_calls,
       CASE WHEN a.metrics->>'call_coverage'='complete' AND t.estimated_input_known=t.llm_calls THEN t.estimated_input_tokens END AS estimated_input_tokens,
       CASE WHEN a.metrics->>'call_coverage'='complete' AND t.estimated_output_known=t.llm_calls THEN t.estimated_output_tokens END AS estimated_output_tokens,
       t.llm_duration_seconds AS known_llm_duration_seconds,
       t.failed_calls AS observed_failed_calls,
       t.estimated_input_tokens AS known_estimated_input_tokens,
       t.estimated_output_tokens AS known_estimated_output_tokens,
       a.metrics,
       c.cascade_location ->> 'detected_origin_agent' AS detected_origin_agent,
       c.cascade_location ->> 'detected_origin_step' AS detected_origin_step,
       c.cascade_location ->> 'detected_origin_event_seq' AS detected_origin_event_seq,
       c.cascade_location ->> 'detected_origin_basis' AS detected_origin_basis,
       c.cascade_location -> 'detected_origin_evidence' AS detected_origin_evidence,
       c.cascade_location -> 'propagation' AS error_propagation,
       c.report #>> '{detector,version}' AS detector_version
FROM public.cm_agent_runs a JOIN public.cm_cases c ON c.id=a.case_id
JOIN public.cm_runs r ON r.id=c.run_id
LEFT JOIN LATERAL (
    SELECT count(*) AS llm_calls,
           count(*) FILTER (WHERE success=false) AS failed_calls,
           sum(duration_ms)/1000 AS llm_duration_seconds,
           count(duration_ms) AS duration_known, count(success) AS success_known,
           count(estimated_input_tokens) AS estimated_input_known,
           count(estimated_output_tokens) AS estimated_output_known,
           count(input_tokens) AS input_known, count(output_tokens) AS output_known,
           sum(input_tokens) AS input_sum, sum(output_tokens) AS output_sum,
           count(*) FILTER (WHERE input_tokens IS NULL OR output_tokens IS NULL) AS missing_usage_calls,
           sum(estimated_input_tokens) AS estimated_input_tokens,
           sum(estimated_output_tokens) AS estimated_output_tokens
    FROM public.cm_llm_calls l WHERE l.agent_run_id=a.id
) t ON true;
