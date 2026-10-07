-- PostgreSQL 14 or later. JSONB retains JSON values, not key order or whitespace.
CREATE TABLE IF NOT EXISTS public.scweet_mcp_tweets (
    tweet_id TEXT COLLATE "C" PRIMARY KEY,
    timestamp TIMESTAMPTZ,
    sort_time TIMESTAMPTZ GENERATED ALWAYS AS
        (coalesce(timestamp, '-infinity'::timestamptz)) STORED,
    author TEXT,
    lang TEXT,
    text TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
);
CREATE INDEX IF NOT EXISTS scweet_mcp_time
    ON public.scweet_mcp_tweets(sort_time DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS scweet_mcp_author
    ON public.scweet_mcp_tweets(lower(author), sort_time DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS scweet_mcp_lang
    ON public.scweet_mcp_tweets(lower(lang), sort_time DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS scweet_mcp_text
    ON public.scweet_mcp_tweets USING GIN(search_vector);
