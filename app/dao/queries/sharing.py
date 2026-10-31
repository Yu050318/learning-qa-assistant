"""Persistence queries for app/service/sharing.py."""
from sqlalchemy import select, inspect, update
from app.entity.do.models import DocumentRecord, DocumentSeries, DocumentVersion, KnowledgeBase, KnowledgeDocument, KnowledgeGrant, Organization, OrganizationMember, User, utcnow
from app.dao.postgres.transactions import unwrap


def workspace_library_ids(oid, session):
    session = unwrap(session)
    return (session.scalars(select(KnowledgeBase.id).where(KnowledgeBase.organization_id == oid).order_by(KnowledgeBase.id)).all())


def get_library(exclusive, kid, lock, session):
    session = unwrap(session)
    query = select(KnowledgeBase).where(KnowledgeBase.id == kid)
    return session.scalar(query.with_for_update(read=not exclusive) if lock else query)


def get_grant(kid, lock, session, uid):
    session = unwrap(session)
    query = select(KnowledgeGrant).where(KnowledgeGrant.knowledge_base_id == kid, KnowledgeGrant.user_id == uid)
    return session.scalar(query.with_for_update(read=True) if lock else query)


def list_organizations(session, uid):
    session = unwrap(session)
    return ([{"id": org.id, "name": org.name, "role": member.role} for org, member in session.execute(
                select(Organization, OrganizationMember).join(OrganizationMember).where(OrganizationMember.user_id == uid, Organization.archived_at.is_(None)).order_by(Organization.name, Organization.id))])


def list_members(oid, session):
    session = unwrap(session)
    return ([{"user_id": user.id, "username": user.username, "role": member.role} for member, user in session.execute(
                select(OrganizationMember, User).join(User).where(OrganizationMember.organization_id == oid).order_by(User.username))])


def find_active_user(session, username):
    session = unwrap(session)
    return (session.scalar(select(User).where(User.username == username, User.is_active.is_(True))))


def lock_user_membership(oid, session, user):
    session = unwrap(session)
    return (session.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == oid, OrganizationMember.user_id == user.id).with_for_update()))


def lock_membership(oid, session, target):
    session = unwrap(session)
    return (session.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == oid, OrganizationMember.user_id == target).with_for_update()))


def user_libraries(session, uid):
    session = unwrap(session)
    return (session.scalars(select(KnowledgeBase).join(OrganizationMember, OrganizationMember.organization_id == KnowledgeBase.organization_id)
                                        .join(Organization, Organization.id == KnowledgeBase.organization_id)
                                        .where(OrganizationMember.user_id == uid, Organization.archived_at.is_(None)).order_by(KnowledgeBase.name, KnowledgeBase.id)).all())


def list_grants(kid, session):
    session = unwrap(session)
    return ([{"user_id": user.id, "username": user.username, "role": grant.role} for grant, user in session.execute(
                select(KnowledgeGrant, User).join(User, User.id == KnowledgeGrant.user_id).where(KnowledgeGrant.knowledge_base_id == kid))])


def document_version(did, session):
    session = unwrap(session)
    return (session.get(DocumentVersion, did) if inspect(session.connection()).has_table("document_versions", schema="rag") else None)


def current_binding(kid, series_id, session):
    session = unwrap(session)
    return (session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid,
                    KnowledgeDocument.series_id == series_id, KnowledgeDocument.is_current.is_(True))))


def clear_current_bindings(kid, series_id, session):
    session = unwrap(session)
    return (session.execute(update(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid,
                    KnowledgeDocument.series_id == series_id).values(is_current=False)))


def library_documents(kid, session):
    session = unwrap(session)
    return (session.execute(select(DocumentRecord, KnowledgeDocument).join(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid)
                                   .order_by(DocumentRecord.original_name, DocumentRecord.id)).all())


def version_numbers(rows, session):
    session = unwrap(session)
    return ({version.document_id: version.version_number for version in session.scalars(select(DocumentVersion)
                .where(DocumentVersion.document_id.in_([row.id for row, _ in rows])))} if inspect(session.connection()).has_table("document_versions", schema="rag") else {})


def owned_documents(include_owned, session, uid):
    session = unwrap(session)
    return (session.scalars(select(DocumentRecord).where(DocumentRecord.user_id == uid, DocumentRecord.organization_id.is_(None))).all() if include_owned else [])


def shared_documents(kids, session):
    session = unwrap(session)
    return (session.execute(select(DocumentRecord, KnowledgeDocument.is_current).join(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id.in_(kids))).all())


def versions_available(versions_enabled, session):
    session = unwrap(session)
    return (versions_enabled or inspect(session.connection()).has_table("document_versions", schema="rag"))


def current_series_documents(documents, session):
    session = unwrap(session)
    return ({version.document_id: current for version, current in session.execute(
                    select(DocumentVersion, DocumentSeries.current_document_id).join(DocumentSeries, DocumentSeries.id == DocumentVersion.series_id)
                    .where(DocumentVersion.document_id.in_(documents)))})
