"""Persistence queries for app/service/hierarchy_backfill.py."""
from sqlalchemy import inspect
from app.entity.do.models import DocumentIndex, DocumentTask, IngestionJob
from app.dao.postgres.transactions import unwrap


def has_immutable_index(document_id, session):
    session = unwrap(session)
    return (inspect(session.connection()).has_table("document_indexes", schema="rag") and session.get(DocumentIndex, document_id) is not None)
