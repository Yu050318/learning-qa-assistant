"""Persistence queries for app/service/ingestion.py."""
from sqlalchemy import inspect
from app.entity.do.models import DocumentRecord, DocumentIndex, DocumentTask, IngestionJob
from app.dao.postgres.transactions import unwrap


def legacy_series_binding_exists(database, document_id):
    database = unwrap(database)
    return (inspect(database.connection()).has_table("document_indexes", schema="rag") and database.get(DocumentIndex, document_id))
