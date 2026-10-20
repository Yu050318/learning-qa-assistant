from uuid import uuid4

from sqlalchemy import select, inspect, update

from app.core.errors import AppError, not_found
from app.infrastructure.persistence.models import (
    DocumentRecord, DocumentSeries, DocumentVersion, KnowledgeBase, KnowledgeDocument,
    KnowledgeGrant, Organization, OrganizationMember, User, utcnow,
)
from app.infrastructure.persistence.repository import Repository


class SharingService:
    def __init__(self, settings, database):
        self.settings, self.database = settings, database

    def require(self):
        if not self.settings.sharing_enabled:
            raise AppError("SHARING_DISABLED", "组织共享尚未启用", 503)

    def member(self, session, uid, oid, *, lock=False):
        return Repository(session).workspace_member(uid, oid, lock=lock)

    def workspace_libraries(self, session, uid, oid, selected=None, *, lock=False):
        self.require()
        self.member(session, uid, oid, lock=lock)
        candidates = session.scalars(select(KnowledgeBase.id).where(KnowledgeBase.organization_id == oid).order_by(KnowledgeBase.id)).all()
        if selected is not None and not set(selected).issubset(candidates):
            raise not_found()
        result = []
        for kid in (candidates if selected is None else dict.fromkeys(selected)):
            try:
                self.access(session, uid, kid, lock=lock)
                result.append(kid)
            except AppError as error:
                if error.status_code != 404 or selected is not None:
                    raise
        return tuple(result)

    def admin(self, session, uid, oid):
        member = self.member(session, uid, oid, lock=True)
        if member.role not in {"owner", "admin"}:
            raise AppError("FORBIDDEN", "需要组织管理权限", 403)
        return member

    def access(self, session, uid, kid, *, write=False, lock=False, exclusive=False):
        query = select(KnowledgeBase).where(KnowledgeBase.id == kid)
        kb = session.scalar(query.with_for_update(read=not exclusive) if lock else query)
        if kb is None:
            raise not_found()
        member = self.member(session, uid, kb.organization_id, lock=lock)
        if member.role in {"owner", "admin"}:
            return kb, "admin"
        query = select(KnowledgeGrant).where(KnowledgeGrant.knowledge_base_id == kid, KnowledgeGrant.user_id == uid)
        grant = session.scalar(query.with_for_update(read=True) if lock else query)
        if grant is None:
            raise not_found()
        if write and grant.role != "editor":
            raise AppError("FORBIDDEN", "需要知识库编辑权限", 403)
        return kb, grant.role

    def organizations(self, uid):
        self.require()
        with self.database.sessions() as session:
            return [{"id": org.id, "name": org.name, "role": member.role} for org, member in session.execute(
                select(Organization, OrganizationMember).join(OrganizationMember).where(OrganizationMember.user_id == uid, Organization.archived_at.is_(None)).order_by(Organization.name, Organization.id))]

    def create_organization(self, uid, name):
        self.require()
        with self.database.sessions() as session:
            org = Organization(id=uuid4(), name=name, owner_id=uid)
            session.add(org);session.flush()
            session.add(OrganizationMember(organization_id=org.id, user_id=uid, role="owner"))
            session.commit()
            return {"id": org.id, "name": org.name, "role": "owner"}

    def members(self, uid, oid):
        self.require()
        with self.database.sessions() as session:
            self.admin(session, uid, oid)
            return [{"user_id": user.id, "username": user.username, "role": member.role} for member, user in session.execute(
                select(OrganizationMember, User).join(User).where(OrganizationMember.organization_id == oid).order_by(User.username))]

    def set_member(self, uid, oid, username, role):
        self.require()
        with self.database.sessions() as session:
            actor = self.admin(session, uid, oid)
            user = session.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
            if user is None:
                raise not_found()
            row = session.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == oid, OrganizationMember.user_id == user.id).with_for_update())
            if (row and row.role == "owner") or role not in {"admin", "member"}:
                raise AppError("FORBIDDEN", "不能修改组织所有者", 403)
            if actor.role != "owner" and (role == "admin" or row and row.role == "admin"):
                raise AppError("FORBIDDEN", "只有所有者可以管理管理员", 403)
            if row is None:
                row = OrganizationMember(organization_id=oid, user_id=user.id)
                session.add(row)
            row.role = role;session.commit()

    def remove_member(self, uid, oid, target):
        self.require()
        with self.database.sessions() as session:
            actor = self.admin(session, uid, oid)
            row = session.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == oid, OrganizationMember.user_id == target).with_for_update())
            if row is None:
                raise not_found()
            if row.role == "owner" or row.role == "admin" and actor.role != "owner":
                raise AppError("FORBIDDEN", "不能移除所有者或越权管理管理员", 403)
            session.delete(row);session.commit()

    def delete_organization(self, uid, oid):
        self.require()
        with self.database.sessions() as session:
            if self.member(session, uid, oid, lock=True).role != "owner":
                raise AppError("FORBIDDEN", "只有所有者可以删除组织", 403)
            org = session.get(Organization, oid)
            org.archived_at = utcnow()
            session.commit()

    def bases(self, uid):
        self.require()
        with self.database.sessions() as session:
            candidates = session.scalars(select(KnowledgeBase).join(OrganizationMember, OrganizationMember.organization_id == KnowledgeBase.organization_id)
                                        .join(Organization, Organization.id == KnowledgeBase.organization_id)
                                        .where(OrganizationMember.user_id == uid, Organization.archived_at.is_(None)).order_by(KnowledgeBase.name, KnowledgeBase.id)).all()
            result = []
            for kb in candidates:
                try:
                    _, role = self.access(session, uid, kb.id)
                except AppError as error:
                    if error.status_code == 404:
                        continue
                    raise
                result.append({"id": kb.id, "name": kb.name, "organization_id": kb.organization_id, "role": role})
            return result

    def create_base(self, uid, oid, name):
        self.require()
        with self.database.sessions() as session:
            self.admin(session, uid, oid)
            kb = KnowledgeBase(id=uuid4(), organization_id=oid, name=name)
            session.add(kb);session.commit()
            return {"id": kb.id, "name": kb.name, "organization_id": oid, "role": "admin"}

    def grants(self, uid, kid):
        self.require()
        with self.database.sessions() as session:
            kb, role = self.access(session, uid, kid, lock=True)
            if role != "admin":
                raise AppError("FORBIDDEN", "需要组织管理权限", 403)
            return [{"user_id": user.id, "username": user.username, "role": grant.role} for grant, user in session.execute(
                select(KnowledgeGrant, User).join(User, User.id == KnowledgeGrant.user_id).where(KnowledgeGrant.knowledge_base_id == kid))]

    def delete_base(self, uid, kid):
        self.require()
        with self.database.sessions() as session:
            kb, role = self.access(session, uid, kid, lock=True, exclusive=True)
            if role != "admin":
                raise AppError("FORBIDDEN", "需要组织管理权限", 403)
            session.delete(kb);session.commit()

    def set_grant(self, uid, kid, target, role):
        self.require()
        with self.database.sessions() as session:
            kb, actor = self.access(session, uid, kid, lock=True, exclusive=True)
            if actor != "admin":
                raise AppError("FORBIDDEN", "需要组织管理权限", 403)
            self.member(session, target, kb.organization_id)
            grant = session.get(KnowledgeGrant, (kid, target))
            if role is None:
                if grant:
                    session.delete(grant)
            else:
                if role not in {"viewer", "editor"}:
                    raise AppError("INVALID_ROLE", "知识库角色无效", 422)
                if grant is None:
                    grant = KnowledgeGrant(knowledge_base_id=kid, user_id=target, organization_id=kb.organization_id)
                    session.add(grant)
                grant.role = role
            session.commit()

    def bind(self, uid, kid, did, *, remove=False, expected_current=None):
        self.require()
        with self.database.sessions() as session:
            library, _ = self.access(session, uid, kid, write=True, lock=True, exclusive=True)
            binding = session.get(KnowledgeDocument, (kid, did))
            if remove:
                if binding is None:
                    raise not_found()
                session.delete(binding)
            else:
                document = Repository(session).document(uid, did, lock=True)
                if document.organization_id is not None and document.organization_id != library.organization_id:
                    raise AppError("INVALID_WORKSPACE_SCOPE", "团队资料不能发布到其他团队空间", 409)
                if document.status != "ready":
                    raise AppError("DOCUMENT_NOT_READY", "只能发布自己已就绪的文档", 409)
                if document.id not in Repository(session).readable_index_ids([document.id], require_index=self.settings.document_publication_enabled):
                    raise AppError("DOCUMENT_INDEX_UNPUBLISHED", "候选索引尚未发布，不能共享", 409)
                version = session.get(DocumentVersion, did) if inspect(session.connection()).has_table("document_versions", schema="rag") else None
                series_id = version.series_id if version else did
                current = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid,
                    KnowledgeDocument.series_id == series_id, KnowledgeDocument.is_current.is_(True)))
                if (current.document_id if current else None) != expected_current:
                    raise AppError("KNOWLEDGE_VERSION_CONFLICT", "共享库当前版本已改变，请刷新后重试", 409)
                session.execute(update(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid,
                    KnowledgeDocument.series_id == series_id).values(is_current=False))
                if binding is None:
                    session.add(KnowledgeDocument(knowledge_base_id=kid, document_id=did, series_id=series_id, is_current=True))
                else:
                    if binding.series_id != series_id:
                        raise AppError("KNOWLEDGE_VERSION_DIVERGED", "共享绑定与文档版本身份不一致", 409)
                    binding.is_current = True
            session.commit()

    def documents(self, uid, kid):
        self.require()
        with self.database.sessions() as session:
            self.access(session, uid, kid)
            rows = session.execute(select(DocumentRecord, KnowledgeDocument).join(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid)
                                   .order_by(DocumentRecord.original_name, DocumentRecord.id)).all()
            readable = Repository(session).readable_index_ids([row.id for row, _ in rows], require_index=self.settings.document_publication_enabled)
            currents = {binding.series_id: row.id for row, binding in rows if binding.is_current}
            versions = {version.document_id: version.version_number for version in session.scalars(select(DocumentVersion)
                .where(DocumentVersion.document_id.in_([row.id for row, _ in rows])))} if inspect(session.connection()).has_table("document_versions", schema="rag") else {}
            return [{"id": row.id, "original_name": row.original_name, "status": row.status,
                     "version_number": versions.get(row.id, 1),
                     "series_id": binding.series_id, "is_current": binding.is_current,
                     "current_document_id": currents.get(binding.series_id),
                     "can_query": row.status == "ready" and (self.settings.document_publication_enabled or row.embedding_model == self.settings.embedding_model) and row.id in readable} for row, binding in rows]

    def scope(self, uid, kids, requested=None, *, session=None, lock=False, include_owned=True, organization_id=None):
        self.require()
        if session is None:
            with self.database.sessions() as current:
                return self.scope(uid, kids, requested, session=current, lock=lock, include_owned=include_owned, organization_id=organization_id)
        if organization_id is not None:
            self.workspace_libraries(session, uid, organization_id, kids, lock=lock)
            include_owned = False
        for kid in kids:
            self.access(session, uid, kid, lock=lock)
        owned = session.scalars(select(DocumentRecord).where(DocumentRecord.user_id == uid, DocumentRecord.organization_id.is_(None))).all() if include_owned else []
        shared_rows = session.execute(select(DocumentRecord, KnowledgeDocument.is_current).join(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id.in_(kids))).all()
        shared = [document for document, _ in shared_rows]
        shared_current = {document.id for document, current in shared_rows if current}
        documents = {doc.id: doc for doc in [*owned, *shared]}
        if requested is not None:
            if not set(requested).issubset(documents):
                raise not_found()
            selected = [documents[did] for did in dict.fromkeys(requested)]
            if any(doc.status != "ready" for doc in selected):
                raise AppError("DOCUMENT_NOT_READY", "指定文档尚未就绪", 409)
            if not self.settings.document_publication_enabled and any(doc.embedding_model != self.settings.embedding_model for doc in selected):
                raise AppError("EMBEDDING_MODEL_MISMATCH", "指定文档使用了不同的向量模型", 409)
        else:
            selected = list(documents.values())
            if self.settings.document_versions_enabled or inspect(session.connection()).has_table("document_versions", schema="rag"):
                bindings = {version.document_id: current for version, current in session.execute(
                    select(DocumentVersion, DocumentSeries.current_document_id).join(DocumentSeries, DocumentSeries.id == DocumentVersion.series_id)
                    .where(DocumentVersion.document_id.in_(documents)))}
                owned_ids = {doc.id for doc in owned}
                selected = [doc for doc in selected if doc.id in shared_current or
                    doc.id in owned_ids and (doc.id not in bindings or bindings[doc.id] == doc.id)]
            else:
                owned_ids = {doc.id for doc in owned}
                selected = [doc for doc in selected if doc.id in shared_current or doc.id in owned_ids]
        readable = Repository(session).readable_index_ids([doc.id for doc in selected], require_index=self.settings.document_publication_enabled)
        if requested is not None and any(doc.id not in readable for doc in selected):
            raise AppError("DOCUMENT_INDEX_UNPUBLISHED", "共享版本的索引尚未发布或已经退休", 409)
        return {doc.id: doc.user_id for doc in selected if doc.status == "ready" and (self.settings.document_publication_enabled or doc.embedding_model == self.settings.embedding_model) and doc.id in readable}
