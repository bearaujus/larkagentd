\getenv collection GROUP_BRAIN_COLLECTION
\getenv dimensions GROUP_BRAIN_EMBEDDING_DIMENSIONS

BEGIN;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
SELECT format(
    'CREATE TABLE IF NOT EXISTS public.%I (id uuid PRIMARY KEY, vector public.vector(%s), payload jsonb)',
    :'collection', :'dimensions'::integer
) \gexec
COMMIT;
