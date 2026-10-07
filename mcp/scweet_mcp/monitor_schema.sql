CREATE TABLE IF NOT EXISTS public.sm_monitors (
    id UUID PRIMARY KEY, author_id TEXT NOT NULL UNIQUE, config JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, enabled BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at TIMESTAMPTZ, last_error JSONB, watermark TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sm_monitor_due ON public.sm_monitors(next_run_at) WHERE enabled;
CREATE TABLE IF NOT EXISTS public.sm_jobs (
    id UUID PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, params JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    lease_token UUID, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT false, checkpoint JSONB NOT NULL DEFAULT '{}',
    progress JSONB NOT NULL DEFAULT '{"pages":0,"fetched":0,"inserted":0,"updated":0}',
    result JSONB, error JSONB
);
CREATE INDEX IF NOT EXISTS sm_jobs_due ON public.sm_jobs(available_at, created_at) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS sm_jobs_continuation ON public.sm_jobs((params->>'resume_from')) WHERE kind='sync_comments';
CREATE TABLE IF NOT EXISTS public.sm_posts (
    post_id TEXT COLLATE "C" PRIMARY KEY, author_id TEXT, published_at TIMESTAMPTZ,
    post_type TEXT NOT NULL, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL, content_hash TEXT NOT NULL,
    next_metric_at TIMESTAMPTZ, next_comments_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sm_posts_author ON public.sm_posts(author_id,published_at DESC,post_id DESC);
CREATE INDEX IF NOT EXISTS sm_posts_due ON public.sm_posts(next_metric_at);
CREATE INDEX IF NOT EXISTS sm_posts_comments_due ON public.sm_posts(next_comments_at);
CREATE TABLE IF NOT EXISTS public.sm_versions (
    entity_id TEXT NOT NULL, kind TEXT NOT NULL, content_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL,
    PRIMARY KEY(kind,entity_id,content_hash)
);
CREATE TABLE IF NOT EXISTS public.sm_snapshots (
    snapshot_id UUID NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL, scheduled_at TIMESTAMPTZ, job_id UUID NOT NULL,
    observation_key TEXT NOT NULL, metrics JSONB NOT NULL, missing_fields JSONB NOT NULL,
    source TEXT NOT NULL, likes BIGINT, comments BIGINT, reposts BIGINT, views BIGINT, quotes BIGINT,
    PRIMARY KEY(observed_at,snapshot_id)
) PARTITION BY RANGE(observed_at);
CREATE TABLE IF NOT EXISTS public.sm_snapshots_default PARTITION OF public.sm_snapshots DEFAULT;
-- Pre-create the current month and the next three. Do not move existing data in the default partition.
DO $$
DECLARE boundary TIMESTAMPTZ; ending TIMESTAMPTZ; partition_name TEXT;
BEGIN
  FOR offset_month IN 0..3 LOOP
    boundary := date_trunc('month', now()) + offset_month * interval '1 month';
    ending := boundary + interval '1 month';
    partition_name := 'sm_snapshots_' || to_char(boundary, 'YYYYMM');
    IF to_regclass('public.' || partition_name) IS NULL AND NOT EXISTS (
      SELECT 1 FROM public.sm_snapshots_default WHERE observed_at >= boundary AND observed_at < ending LIMIT 1
    ) THEN
      EXECUTE format('CREATE TABLE public.%I PARTITION OF public.sm_snapshots FOR VALUES FROM (%L) TO (%L)', partition_name, boundary, ending);
    END IF;
  END LOOP;
END $$;
CREATE INDEX IF NOT EXISTS sm_snapshot_entity ON public.sm_snapshots(entity_id,kind,observed_at DESC,snapshot_id DESC);
CREATE TABLE IF NOT EXISTS public.sm_observations (
    job_id UUID NOT NULL, observation_key TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
    PRIMARY KEY(job_id,observation_key,entity_id,kind)
);
CREATE TABLE IF NOT EXISTS public.sm_comments (
    comment_id TEXT COLLATE "C" PRIMARY KEY, root_post_id TEXT NOT NULL, parent_comment_id TEXT,
    author_id TEXT, published_at TIMESTAMPTZ, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL, content_hash TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'visible'
);
CREATE INDEX IF NOT EXISTS sm_comments_root ON public.sm_comments(root_post_id,published_at,comment_id);
CREATE INDEX IF NOT EXISTS sm_comments_seen ON public.sm_comments(root_post_id,first_seen_at,comment_id);
CREATE INDEX IF NOT EXISTS sm_comments_parent ON public.sm_comments(parent_comment_id);
CREATE INDEX IF NOT EXISTS sm_comments_author ON public.sm_comments(root_post_id,author_id,first_seen_at,comment_id);
CREATE TABLE IF NOT EXISTS public.sm_coverage (
    root_post_id TEXT NOT NULL, scope TEXT NOT NULL, job_id UUID NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL, coverage JSONB NOT NULL, PRIMARY KEY(root_post_id,scope)
);
CREATE TABLE IF NOT EXISTS public.sm_accounts (
    id UUID PRIMARY KEY, credential_ref TEXT NOT NULL UNIQUE, label TEXT NOT NULL, proxy_ref TEXT,
    platform_user_id TEXT, enabled BOOLEAN NOT NULL DEFAULT true, health TEXT NOT NULL DEFAULT 'unchecked',
    checked_at TIMESTAMPTZ, cooldown_until TIMESTAMPTZ, capabilities JSONB NOT NULL DEFAULT '{}',
    reason_code TEXT, lease_token UUID, lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS sm_account_identity ON public.sm_accounts(platform_user_id)
    WHERE platform_user_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS public.sm_account_requests (
    id BIGSERIAL PRIMARY KEY, account_id UUID NOT NULL, requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    tweets INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sm_account_window ON public.sm_account_requests(account_id,requested_at);
CREATE TABLE IF NOT EXISTS public.sm_events (
    id UUID PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
    monitor_id UUID, post_id TEXT, observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS sm_events_time ON public.sm_events(observed_at,id);
CREATE TABLE IF NOT EXISTS public.sm_event_acks (
    consumer_ref TEXT NOT NULL, event_id UUID NOT NULL REFERENCES public.sm_events(id) ON DELETE CASCADE,
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(consumer_ref,event_id)
);
CREATE TABLE IF NOT EXISTS public.sm_deliveries (
    event_id UUID NOT NULL REFERENCES public.sm_events(id) ON DELETE CASCADE,
    destination_ref TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), lease_until TIMESTAMPTZ, lease_token UUID,
    last_error TEXT, PRIMARY KEY(event_id,destination_ref)
);
CREATE TABLE IF NOT EXISTS public.sm_exports (
    id UUID PRIMARY KEY, job_id UUID NOT NULL UNIQUE, relative_path TEXT NOT NULL, manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.sm_analysis_runs (
    job_id UUID PRIMARY KEY, kind TEXT NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.sm_schema_version (version INTEGER PRIMARY KEY);
CREATE INDEX IF NOT EXISTS sm_analysis_author ON public.sm_analysis_runs ((result->>'author_id'),created_at DESC,job_id DESC);
INSERT INTO public.sm_schema_version VALUES(1) ON CONFLICT DO NOTHING;

-- One archive per platform user ID. Categories remain in their indexed business tables.
CREATE TABLE IF NOT EXISTS public.sm_authors (
    author_id TEXT PRIMARY KEY, handle TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sm_authors_handle ON public.sm_authors(lower(handle),author_id);
CREATE TABLE IF NOT EXISTS public.sm_author_posts (
    post_id TEXT PRIMARY KEY, author_id TEXT NOT NULL REFERENCES public.sm_authors(author_id)
);
CREATE INDEX IF NOT EXISTS sm_author_posts_author ON public.sm_author_posts(author_id,post_id);
CREATE TABLE IF NOT EXISTS public.sm_author_operations (
    author_id TEXT NOT NULL REFERENCES public.sm_authors(author_id),
    job_id UUID NOT NULL REFERENCES public.sm_jobs(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(author_id,job_id)
);
CREATE INDEX IF NOT EXISTS sm_author_history ON public.sm_author_operations(author_id,created_at DESC,job_id DESC);
-- Add links to existing data without moving, copying or deleting source payloads.
DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM public.sm_schema_version WHERE version=2) THEN
INSERT INTO public.sm_authors(author_id)
    SELECT DISTINCT author_id FROM public.sm_posts WHERE author_id ~ '^[0-9]{1,32}$'
    UNION SELECT author_id FROM public.sm_monitors WHERE author_id ~ '^[0-9]{1,32}$'
    UNION SELECT params->>'author_id' FROM public.sm_jobs WHERE params->>'author_id' ~ '^[0-9]{1,32}$'
    UNION SELECT checkpoint->>'author_id' FROM public.sm_jobs WHERE checkpoint->>'author_id' ~ '^[0-9]{1,32}$'
    UNION SELECT params->>'target' FROM public.sm_jobs WHERE params->>'target' ~ '^[0-9]{1,32}$'
    UNION SELECT params->'config'->>'author_id' FROM public.sm_jobs WHERE params->'config'->>'author_id' ~ '^[0-9]{1,32}$'
    ON CONFLICT DO NOTHING;
UPDATE public.sm_authors a SET handle=p.handle FROM (
    SELECT DISTINCT ON (author_id) author_id,lower(payload->'user'->>'screen_name') AS handle
    FROM public.sm_posts WHERE payload->'user'->>'screen_name' ~ '^[A-Za-z0-9_]{1,15}$'
    ORDER BY author_id,last_observed_at DESC
) p WHERE a.author_id=p.author_id AND a.handle IS NULL;
INSERT INTO public.sm_author_posts(post_id,author_id)
    SELECT post_id,author_id FROM public.sm_posts WHERE author_id ~ '^[0-9]{1,32}$'
    ON CONFLICT DO NOTHING;
INSERT INTO public.sm_author_operations(author_id,job_id,created_at)
    SELECT DISTINCT p.author_id,j.id,j.created_at FROM public.sm_jobs j
    JOIN public.sm_author_posts p ON p.post_id=j.params->>'post_id'
    UNION SELECT p.author_id,j.id,j.created_at FROM public.sm_jobs j
        CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(j.params->'post_ids','[]')) ids(post_id)
        JOIN public.sm_author_posts p ON p.post_id=ids.post_id
    UNION SELECT a.author_id,j.id,j.created_at FROM public.sm_jobs j JOIN public.sm_authors a
        ON a.author_id=coalesce(j.checkpoint->>'author_id',j.params->>'author_id',j.params->>'target')
    UNION SELECT m.author_id,j.id,j.created_at FROM public.sm_jobs j JOIN public.sm_monitors m
        ON m.id::text=j.params->>'monitor_id'
    UNION SELECT a.author_id,j.id,j.created_at FROM public.sm_jobs j JOIN public.sm_authors a
        ON a.author_id=j.params->'config'->>'author_id'
    ON CONFLICT DO NOTHING;
INSERT INTO public.sm_schema_version VALUES(2) ON CONFLICT DO NOTHING;
END IF;
END $$;
-- Additive state tracking. A missing lookup never removes collected content.
ALTER TABLE public.sm_posts ADD COLUMN IF NOT EXISTS availability TEXT NOT NULL DEFAULT 'visible';
ALTER TABLE public.sm_posts ADD COLUMN IF NOT EXISTS availability_checked_at TIMESTAMPTZ;
ALTER TABLE public.sm_posts ADD COLUMN IF NOT EXISTS last_lookup_scheduled_at TIMESTAMPTZ;
ALTER TABLE public.sm_posts ADD COLUMN IF NOT EXISTS missing_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS sm_posts_check_rotation ON public.sm_posts(author_id,availability_checked_at,post_id);
CREATE INDEX IF NOT EXISTS sm_posts_lookup_rotation ON public.sm_posts(author_id,(greatest(availability_checked_at,last_lookup_scheduled_at)),post_id);
CREATE INDEX IF NOT EXISTS sm_events_monitor_time ON public.sm_events(monitor_id,observed_at,id);
INSERT INTO public.sm_schema_version VALUES(3) ON CONFLICT DO NOTHING;
