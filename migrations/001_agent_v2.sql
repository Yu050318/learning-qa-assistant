BEGIN;
SET LOCAL lock_timeout = '3s';
SELECT pg_advisory_xact_lock(hashtext('rag_v1.agent_v2'));

CREATE TABLE IF NOT EXISTS rag_v1.schema_migrations (
    version VARCHAR(100) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE rag_v1.chat_sessions
    ADD COLUMN IF NOT EXISTS api_version VARCHAR(2) NOT NULL DEFAULT 'v1';
ALTER TABLE rag_v1.chat_messages
    ADD COLUMN IF NOT EXISTS run_metadata JSONB NULL;
ALTER TABLE rag_v1.documents
    ADD COLUMN IF NOT EXISTS parser_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$
DECLARE
    invalid_columns integer;
BEGIN
    SELECT count(*) INTO invalid_columns
    FROM (VALUES
        ('chat_sessions', 'api_version', 'character varying', 'NO', '''v1''::character varying'),
        ('chat_messages', 'run_metadata', 'jsonb', 'YES', NULL),
        ('documents', 'parser_metadata', 'jsonb', 'NO', '''{}''::jsonb')
    ) AS expected(table_name, column_name, data_type, nullable, default_value)
    LEFT JOIN information_schema.columns actual
      ON actual.table_schema = 'rag_v1'
     AND actual.table_name = expected.table_name
     AND actual.column_name = expected.column_name
    WHERE actual.column_name IS NULL
       OR actual.data_type <> expected.data_type
       OR actual.is_nullable <> expected.nullable
       OR (expected.default_value IS NOT NULL AND actual.column_default <> expected.default_value);
    IF invalid_columns <> 0 THEN
        RAISE EXCEPTION 'agent-v2 migration schema mismatch';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_sessions_api_version'
          AND conrelid = 'rag_v1.chat_sessions'::regclass
    ) THEN
        ALTER TABLE rag_v1.chat_sessions
            ADD CONSTRAINT ck_sessions_api_version CHECK (api_version IN ('v1', 'v2'));
    END IF;
END $$;

INSERT INTO rag_v1.schema_migrations(version)
VALUES ('001_agent_v2')
ON CONFLICT (version) DO NOTHING;
COMMIT;
