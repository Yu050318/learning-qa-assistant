"""Persistence queries for app/service/document_publication.py."""
from sqlalchemy import select
from app.entity.do.models import DocumentRecord, DocumentIndex, DocumentSeries, DocumentVersion, utcnow
from app.dao.postgres.transactions import unwrap


def documents_for_snapshot(owners, session, lock=False):
    session = unwrap(session)
    statement = select(DocumentRecord).where(DocumentRecord.id.in_(owners)).order_by(DocumentRecord.id)
    return session.scalars(statement.with_for_update() if lock else statement).all()


def indexes_by_document(owners, session):
    session = unwrap(session)
    return ({row.document_id: row for row in session.scalars(select(DocumentIndex).where(DocumentIndex.document_id.in_(owners)))})
