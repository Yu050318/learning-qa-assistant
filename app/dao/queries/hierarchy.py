"""Persistence queries for app/service/hierarchy.py."""
from sqlalchemy import delete, insert, select, text
from app.entity.do.models import DocumentRecord, HierarchyNode
from app.dao.postgres.transactions import unwrap


def remove_document_nodes(document, session):
    session = unwrap(session)
    return (session.execute(delete(HierarchyNode).where(HierarchyNode.document_id == document.id, HierarchyNode.user_id == document.user_id)))


def insert_node_batch(offset, rows, session):
    session = unwrap(session)
    return (session.execute(insert(HierarchyNode), rows[offset:offset + 200]))


def set_query_timeout(remaining, session):
    session = unwrap(session)
    return (session.execute(text("SELECT set_config('statement_timeout', :value, true)"), {"value": str(max(1, int(remaining * 1000)))}))


def fetch_rows(session, statement, timeout):
    if session.dialect_name == 'postgresql':
        set_query_timeout(timeout, session)
    return list(unwrap(session).scalars(statement))


def documents_for_hits(session, user_id, document_ids, timeout):
    return fetch_rows(session, select(DocumentRecord).where(DocumentRecord.user_id == user_id,
        DocumentRecord.id.in_(document_ids)), timeout)


def scoped_nodes(session, user_id, document_ids, node_ids, limit, timeout, *, by_parent=False):
    if not node_ids:
        return []
    column = HierarchyNode.parent_id if by_parent else HierarchyNode.node_id
    return fetch_rows(session, select(HierarchyNode).where(HierarchyNode.user_id == user_id,
        HierarchyNode.document_id.in_(document_ids), column.in_(node_ids)).limit(limit + 1), timeout)
