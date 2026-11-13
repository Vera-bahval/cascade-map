-- Independent human annotations. Base result tables keep their original schema.
CREATE TABLE IF NOT EXISTS public.cm_case_reviews (
    id uuid PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES public.cm_cases(id) ON DELETE CASCADE,
    reviewer_key text,
    reviewed_at timestamptz,
    schema_version integer NOT NULL DEFAULT 1,
    assessment jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS cm_case_reviews_case_idx ON public.cm_case_reviews(case_id, reviewed_at);
