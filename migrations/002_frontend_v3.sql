BEGIN;
SET LOCAL lock_timeout = '3s';
SELECT pg_advisory_xact_lock(hashtext('rag_v1.frontend_v3'));

ALTER TABLE rag_v1.documents ADD COLUMN IF NOT EXISTS size_bytes BIGINT NULL;

INSERT INTO rag_v1.schema_migrations(version)
VALUES ('002_frontend_v3')
ON CONFLICT (version) DO NOTHING;
COMMIT;
