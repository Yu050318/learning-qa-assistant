from app.entity.execution import FROZEN_WORKFLOW_SETTINGS
from app.manager.transactions import transaction_scope, as_transaction
import logging
import re
import hashlib
import json
from time import monotonic, perf_counter
from uuid import UUID, uuid4
from copy import copy
from contextlib import closing, nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.service.sessions import SessionService
from app.service.sharing import SharingService
from app.service.model_center import ModelCenter
from app.manager.evidence import count_tokens, evidence_token_budget
from app.core.config import Settings
from app.core.errors import AppError
from app.entity.bo.contracts import AgentChatInput, ChatTurn, RunContext
from app.entity.bo.retrieval_scope import RetrievalIndex, retrieval_index_identity
from app.service.document_publication import DocumentPublication
from app.dao.postgres.database import Database
from app.entity.do.models import ChatMessage, RunIndexPin, User, utcnow
from app.dao.postgres.repository import Repository
from app.manager.agent_runtime import SYSTEM_PROMPT as AGENT_SYSTEM_PROMPT

logger = logging.getLogger("rag")

# Credentials and platform connection strings are deliberately not part of a
# persisted snapshot. Workers resolve credentials from their server environment.

_SENSITIVE_PUBLIC_QUERY = re.compile(
    r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S{8,}"
)


def workflow_settings_snapshot(settings):
    for name in ("deepseek_base_url", "ollama_base_url", "rerank_endpoint"):
        endpoint = urlsplit(getattr(settings, name))
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise AppError("INVALID_MODEL_ENDPOINT", "模型地址不可包含凭据、查询串或片段", 503)
    return {name: getattr(settings, name) for name in FROZEN_WORKFLOW_SETTINGS}


def prepare_public_query(payload: AgentChatInput, web_enabled: bool) -> tuple[str | None, bool, tuple[str, ...]]:
    if payload.search_mode in {"knowledge", "sql"}:
        return None, False, ()
    query = (payload.web_query or payload.question).strip()
    blocked = len(query) > 400 or _SENSITIVE_PUBLIC_QUERY.search(query) is not None
    if payload.web_urls and payload.web_query is None:
        return None, web_enabled, ()
    if payload.search_mode == "web":
        if blocked:
            raise AppError("WEB_QUERY_REQUIRED", "联网查询过长或包含明显敏感配置，请提供安全的 web_query", 422)
        return query, web_enabled, ()
    if blocked:
        return None, False, ("WEB_QUERY_BLOCKED",)
    warnings = () if web_enabled else ("WEB_SEARCH_DISABLED",)
    return query, web_enabled, warnings


class ChatService:
    def completed_message(self, user_id, run):
        from app.dao.postgres.chat_queries import message
        with transaction_scope(self.database) as transaction:
            Repository(transaction).chat_session(user_id, run.session_id, kind="chat")
            result = message(transaction, user_id, run.message_id)
            if result is None:
                raise AppError("NOT_FOUND", "消息不存在", 404)
            return result

    def __init__(self, settings: Settings, database: Database, sessions: SessionService, agent_workflow):
        self.settings = settings
        self.database = database
        self.sessions = sessions
        self.agent_workflow = agent_workflow
        self.sharing = SharingService(settings, database)
        from app.service.capabilities import CapabilityService
        self.capabilities = CapabilityService(settings, database)
        self.model_center = ModelCenter(settings, database) if settings.model_center_enabled else None

    def document_scope(self, repository, user_id, session, payload, settings):
        requested = list(payload.document_ids) if payload.document_ids is not None else None
        kids = tuple(payload.knowledge_base_ids)
        team = session.organization_id is not None
        if team:
            # The session's immutable workspace, never a client-provided tenant,
            # determines the eligible libraries. No grants means an empty scope.
            kids = self.sharing.workspace_libraries(repository.database, user_id, session.organization_id,
                kids or None) if payload.search_mode not in {"web", "sql"} else ()
        elif settings.workspaces_enabled and session.kind == "chat" and kids:
            raise AppError("INVALID_WORKSPACE_SCOPE", "请在团队空间中使用共享知识库", 422)
        version_options = {"current_only": True} if settings.document_versions_enabled else {}
        ready_ids = [] if payload.search_mode in {"web", "sql"} or kids or team else repository.ready_document_ids(
            user_id, requested, settings.embedding_model, require_index=settings.document_publication_enabled, **version_options)
        owners = {}
        if kids:
            if payload.search_mode in {"web", "sql"}:
                raise AppError("INVALID_SCOPE", "此模式不接受共享知识库", 422)
            owners = self.sharing.scope(user_id, kids, requested, session=repository.database,
                organization_id=session.organization_id, include_owned=not (team or session.kind == "evaluation"))
            ready_ids = list(owners)
        elif team and requested:
            raise AppError("DOCUMENT_SCOPE_CHANGED", "团队空间没有授权这些资料", 409)
        indexes = DocumentPublication(settings, None).freeze(repository.database,
            owners or {did: user_id for did in ready_ids}) if settings.document_publication_enabled else ()
        return ready_ids, owners, indexes, kids

    def scope_and_history(self, repository, user_id, session, payload, settings):
        ready_ids, owners, indexes, kids = self.document_scope(repository, user_id, session, payload, settings)
        history = [ChatTurn(message.role, message.content) for message in repository.context_messages(
            user_id, session.id)[-settings.memory_recent_messages:]]
        return ready_ids, owners, history, session.summary, indexes, kids

    def prepare_run(self, database, run, payload, *, frozen_snapshot=None, frozen_capabilities=None):
        """Run, frozen inputs and one stable message pair share the create transaction."""
        original_question = payload.question
        if self.settings.capability_center_enabled and frozen_capabilities is None:
            question, skill_name = self.capabilities.activation(payload.question, payload.skill_name)
            payload = replace(payload, question=question, skill_name=skill_name)
        repository = Repository(database)
        session = repository.chat_session(run.user_id, run.session_id, lock=True)
        if payload.model_profile_id is not None and self.model_center is None:
            raise AppError("MODEL_CENTER_DISABLED", "Profile 选择要求启用模型中心", 503)
        snapshot = frozen_snapshot or (self.model_center.snapshot(payload.model_provider, session=database, profile_id=payload.model_profile_id) if self.model_center else None)
        settings = snapshot.settings if snapshot else self.settings
        selected = payload.model_provider or settings.default_model_provider
        if selected not in settings.enabled_model_providers:
            raise AppError("MODEL_DISABLED", "该模型已停用", 403)
        public_query, web_enabled, warnings = prepare_public_query(payload, settings.web_search_enabled)
        capability_snapshot = None
        approval = None
        if frozen_capabilities is not None:
            execution = self.capabilities.validate_execution(run.user_id, frozen_capabilities, session=database)
            if execution.approved_tools or payload.approved_tools or payload.approval_catalog_revision is not None:
                raise AppError("INVALID_TOOL_APPROVAL", "评测或外部冻结任务不能代替用户批准高风险工具", 403)
            if execution.search_mode != payload.search_mode or execution.skill_name != payload.skill_name:
                raise AppError("CAPABILITY_SCOPE_CHANGED", "任务冻结的能力与本题请求不一致", 409)
            capability_snapshot = execution.model_dump(mode="json")
        elif self.settings.capability_center_enabled:
            capability_snapshot = self.capabilities.freeze(run.user_id, skill_name=payload.skill_name,
                search_mode=payload.search_mode, public_query=public_query, approved_tools=payload.approved_tools,
                approval_catalog_revision=payload.approval_catalog_revision, session=database).model_dump(mode="json")
        elif payload.skill_name is not None:
            raise AppError("CAPABILITIES_DISABLED", "能力中心尚未启用", 503)
        elif payload.approved_tools or payload.approval_catalog_revision is not None:
            raise AppError("CAPABILITIES_DISABLED", "工具批准要求启用能力中心", 503)
        if capability_snapshot is not None and capability_snapshot["approved_tools"]:
            from app.entity.bo.tool_approvals import RunToolApproval
            approval = RunToolApproval(user_id=run.user_id, session_id=run.session_id, run_id=run.id,
                organization_id=session.organization_id, catalog_hash=capability_snapshot["catalog_hash"],
                tool_names=tuple(capability_snapshot["approved_tools"])).model_dump(mode="json")
        if capability_snapshot is not None and "delegate_web" not in capability_snapshot["allowed_tools"]:
            settings = settings.model_copy(update={"web_search_enabled": False})
            public_query, web_enabled, warnings = prepare_public_query(payload, False)
        frozen_settings = workflow_settings_snapshot(settings)
        has_http_tools = capability_snapshot is not None and any(
            name in capability_snapshot["catalog"]["tools"] for name in capability_snapshot["allowed_tools"])
        if payload.search_mode == "web" and (not settings.web_search_enabled and not payload.web_urls or capability_snapshot is not None and "delegate_web" not in capability_snapshot["allowed_tools"]) and not has_http_tools:
            raise AppError("WEB_SEARCH_UNAVAILABLE", "网络搜索尚未启用", 503)
        ready_ids, owners, history, summary, indexes, kids = self.scope_and_history(repository, run.user_id, session, payload, settings)
        message_time = utcnow()
        user_message = ChatMessage(user_id=run.user_id, session_id=run.session_id, role="user", content=original_question,
                                   created_at=message_time)
        assistant = ChatMessage(user_id=run.user_id, session_id=run.session_id, role="assistant", content="",
                                run_metadata={"run_id": str(run.id), "state": "queued"},
                                created_at=message_time + timedelta(microseconds=1))
        as_transaction(database).save_all([user_message, assistant])
        database.flush()
        run.user_message_id, run.message_id = user_message.id, assistant.id
        context = RunContext(request_id=run.request_id, user_id=run.user_id, session_id=run.session_id,
            question=payload.question, search_mode=payload.search_mode, allowed_document_ids=tuple(ready_ids),
            selected_provider=selected, public_query=public_query, web_enabled=web_enabled, warnings=warnings,
            web_urls=payload.web_urls, source_message_id=user_message.id, started_at_utc=run.created_at,
            knowledge_base_ids=kids, document_owners=tuple(owners.items()), retrieval_indexes=indexes,
            organization_id=session.organization_id, shared_only=session.organization_id is not None or session.kind == "evaluation")
        context = replace(context, capability_snapshot=capability_snapshot, run_id=run.id, tool_approval=approval)
        context_data = asdict(context)
        for target in indexes:
            as_transaction(database).save(RunIndexPin(run_id=run.id, document_id=target.document_id))
        context_data.pop("deadline_monotonic")
        run.execution_snapshot = json.loads(json.dumps({"version": 1, "context": context_data,
            "history": [asdict(turn) for turn in history], "summary": summary,
            "settings": frozen_settings,
            "retrieval_index_sha256": retrieval_index_identity(settings) if ready_ids else None,
            "model_center": {"revision": snapshot.revision, "configuration": snapshot.configuration.model_dump(mode="json"),
                "catalog": snapshot.catalog.model_dump(mode="json") if snapshot.catalog else None,
                "answer_profile_id": snapshot.answer_profile_id} if snapshot else None,
        }, default=str))
        session.updated_at = utcnow()

    def execute_prepared(self, store, claim, on_answer_event=None, *, flush_answer=None, validate_external=None):
        """Execute a claimed snapshot and update its original assistant message."""
        from app.manager.run_checkpoint import RunCheckpoint
        from app.manager.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
        from app.entity.time import aware

        with transaction_scope(self.database) as database:
            run = store.owned(database, claim)
            snapshot, session_id = run.execution_snapshot, run.session_id
            evaluation = run.execution_kind == "evaluation"
            message_id, user_message_id = run.message_id, run.user_message_id
            remaining = (aware(run.expires_at) - store.now(database)).total_seconds()
        if snapshot.get("version") != 1 or not message_id or not user_message_id:
            raise AppError("RUN_CHECKPOINT_DIVERGED", "运行快照不完整，无法恢复", 409)
        settings = self.settings.model_copy(update={name: snapshot["settings"][name] for name in FROZEN_WORKFLOW_SETTINGS})
        values = dict(snapshot["context"])
        for name in ("user_id", "session_id", "source_message_id"):
            values[name] = UUID(values[name])
        if values.get("run_id") is not None:
            values["run_id"] = UUID(values["run_id"])
        for name in ("allowed_document_ids", "knowledge_base_ids"):
            values[name] = tuple(UUID(value) for value in values[name])
        values["document_owners"] = tuple((UUID(did), UUID(uid)) for did, uid in values["document_owners"])
        try:
            if values.get("organization_id") is not None:
                values["organization_id"] = UUID(values["organization_id"])
            values["retrieval_indexes"] = tuple(RetrievalIndex.from_payload(target) for target in values.get("retrieval_indexes", ()))
        except (ValueError, TypeError, KeyError):
            raise AppError("RUN_CHECKPOINT_DIVERGED", "冻结索引快照无效", 409) from None
        values["started_at_utc"] = datetime.fromisoformat(values["started_at_utc"])
        values["web_urls"], values["warnings"] = tuple(values["web_urls"]), tuple(values["warnings"])
        values["deadline_monotonic"] = monotonic() + remaining
        try:
            context = RunContext(**values)
        except (TypeError, ValueError):
            raise AppError("RUN_CHECKPOINT_DIVERGED", "运行范围与冻结索引不一致", 409) from None
        if settings.document_publication_enabled and context.allowed_document_ids and not context.retrieval_indexes:
            raise AppError("RUN_CHECKPOINT_DIVERGED", "旧运行缺少版本化索引范围，请重新发起问答", 409)
        if (context.user_id != claim.user_id or context.session_id != session_id
                or context.source_message_id != user_message_id or context.run_id not in {None, claim.run_id}):
            raise AppError("RUN_CHECKPOINT_DIVERGED", "运行身份与快照不一致", 409)
        if self.settings.capability_center_enabled and context.capability_snapshot is None:
            raise AppError("RUN_CHECKPOINT_DIVERGED", "旧运行缺少冻结能力版本，请重新发起问答", 409)
        workflow = copy(self.agent_workflow)
        run_sql = None
        if context.capability_snapshot is not None:
            execution = self.capabilities.validate_execution(claim.user_id, context.capability_snapshot)
            self.capabilities.validate_approval(execution, context.tool_approval, user_id=claim.user_id,
                session_id=session_id, run_id=claim.run_id, organization_id=context.organization_id)
            if "delegate_sql" in execution.allowed_tools:
                from app.manager.sql import ReadOnlySQL
                settings = self.capabilities.project_sql(settings, execution.sql_policy)
                run_sql = ReadOnlySQL(settings)
                workflow.sql = run_sql
        workflow.settings = settings
        if getattr(workflow, "retriever", None) is not None and hasattr(workflow.retriever, "for_settings"):
            workflow.retriever = workflow.retriever.for_settings(settings)
        catalog = (snapshot.get("model_center") or {}).get("catalog")
        if catalog:
            from app.entity.bo.model_catalog import ModelCatalog
            from app.manager.llms.catalog import CatalogRouter
            workflow.models = CatalogRouter(settings, ModelCatalog.model_validate(catalog),
                profile_id=snapshot["model_center"]["answer_profile_id"])
        else:
            workflow.models = ModelRouter(settings, {"deepseek": DeepSeekProvider(settings), "ollama": OllamaProvider(settings)})
        sharing = SharingService(settings, self.database)

        def validate(database=None, *, lock=False):
            if validate_external is not None:
                validate_external()
            expected_index = snapshot.get("retrieval_index_sha256")
            if not context.retrieval_indexes and expected_index is not None and expected_index != retrieval_index_identity(self.settings):
                raise AppError("DOCUMENT_SCOPE_CHANGED", "知识库索引配置已变化，请重新发起问答", 409)
            from contextlib import nullcontext
            with (nullcontext(database) if database is not None else transaction_scope(self.database)) as database:
                if context.capability_snapshot is not None:
                    execution = self.capabilities.validate_execution(claim.user_id, context.capability_snapshot, session=database, lock=lock)
                    self.capabilities.validate_approval(execution, context.tool_approval, user_id=claim.user_id,
                        session_id=session_id, run_id=claim.run_id, organization_id=context.organization_id)
                user = as_transaction(database).load(User, claim.user_id)
                if user is None or not user.is_active:
                    raise AppError("FORBIDDEN", "账号已停用", 403)
                repository = Repository(database)
                if context.retrieval_indexes:
                    DocumentPublication(self.settings, None).validate_targets(database, context.retrieval_indexes, lock=lock)
                try:
                    current_session = repository.chat_session(claim.user_id, session_id, lock=lock)
                except AppError as error:
                    if context.organization_id is not None and error.status_code in {403, 404}:
                        raise AppError("DOCUMENT_SCOPE_CHANGED", "团队空间授权已撤销或已归档", 409) from None
                    raise
                if current_session.organization_id != context.organization_id:
                    raise AppError("DOCUMENT_SCOPE_CHANGED", "会话空间与冻结范围不一致", 409)
                for mid, role in ((user_message_id, "user"), (message_id, "assistant")):
                    message = as_transaction(database).load(ChatMessage, mid)
                    if message is None or (message.user_id, message.session_id, message.role) != (claim.user_id, session_id, role):
                        raise AppError("RUN_CHECKPOINT_DIVERGED", "运行消息已变化", 409)
                frozen = list(context.allowed_document_ids)
                if context.knowledge_base_ids:
                    try:
                        current = sharing.scope(claim.user_id, context.knowledge_base_ids, frozen, session=database, lock=lock,
                            organization_id=context.organization_id, include_owned=not context.shared_only)
                    except AppError as error:
                        if error.status_code in {403, 404}:
                            raise AppError("DOCUMENT_SCOPE_CHANGED", "共享授权已撤销", 409) from None
                        raise
                    if current != dict(context.document_owners):
                        raise AppError("DOCUMENT_SCOPE_CHANGED", "共享授权或文档范围已变化", 409)
                else:
                    current = repository.ready_document_ids(claim.user_id, frozen, settings.embedding_model, require_index=settings.document_publication_enabled)
                    if set(current) != set(frozen):
                        raise AppError("DOCUMENT_SCOPE_CHANGED", "文档范围已变化", 409)

        checkpoint = RunCheckpoint(store, claim, validate=validate)
        options = {"checkpoint": checkpoint}
        captured = []
        if evaluation:
            options["on_evidence"] = captured.extend
        if self.model_center and snapshot["model_center"]:
            revision = snapshot["model_center"]["revision"]
            options["call_observer"] = self.model_center.observer(claim.user_id, session_id, context.request_id, revision)
        with self.sessions.exclusive(session_id) as probe, (closing(workflow.retriever)
                if getattr(workflow, "retriever", None) is not getattr(self.agent_workflow, "retriever", None) else nullcontext()), (closing(run_sql) if run_sql is not None else nullcontext()):
            checkpoint.check()
            result = workflow.run(context, [ChatTurn(**turn) for turn in snapshot["history"]], snapshot["summary"],
                                  on_answer_event=on_answer_event, **options)
            if context.capability_snapshot is not None:
                frozen = context.capability_snapshot
                name = frozen["skill_name"]
                result.run_metadata["capabilities"] = {"revision": frozen["revision"], "catalog_hash": frozen["catalog_hash"],
                    "skill": {"name": name, "version": frozen["catalog"]["skills"][name]["version"]} if name else None}
            if flush_answer:
                flush_answer()
            checkpoint.check()
            if probe:
                probe()
            with transaction_scope(self.database) as database:
                # Creation holds the session before inspecting its active Run.
                Repository(database).chat_session(claim.user_id, session_id, lock=True)
                current_run = store.owned(database, claim)
                validate(database, lock=True)
                cited = {UUID(item["document_id"]) for item in result.citations if item["type"] == "knowledge"}
                if not cited.issubset(context.allowed_document_ids):
                    raise AppError("DOCUMENT_SCOPE_CHANGED", "引用超出冻结文档范围", 409)
                if settings.document_versions_enabled and cited:
                    versions, owners = {}, dict(context.document_owners)
                    for owner in {owners.get(did, claim.user_id) for did in cited}:
                        versions.update(Repository(database).version_sources(owner, [did for did in cited if owners.get(did, claim.user_id) == owner]))
                    for citation in result.citations:
                        if citation["type"] == "knowledge":
                            citation["document_version"] = versions[citation["document_id"]]
                    for item in captured:
                        if item["citation"]["type"] == "knowledge" and item["citation"]["document_id"] in versions:
                            item["citation"]["document_version"] = versions[item["citation"]["document_id"]]
                if evaluation:
                    from app.core.serialization import bounded_json
                    current_run.result_snapshot = bounded_json({"kind": result.kind, "evidence": captured}, 4_000_000)
                message = as_transaction(database).load(ChatMessage, message_id)
                message.content, message.model_provider, message.model_name = result.answer.content, result.answer.provider, result.answer.model
                message.citations, message.token_usage = result.citations, result.answer.token_usage or None
                message.run_metadata = {**result.run_metadata, "run_id": str(claim.run_id),
                                        "state": "waiting" if result.kind == "clarification" else "succeeded"}
                if self.settings.openviking_enabled and not evaluation:
                    from app.service.automatic_memory import AutomaticMemoryService
                    AutomaticMemoryService(self.settings, self.database, None).validate_citations(database, claim.user_id, result.citations)
                    if result.kind != "clarification":
                        from app.dao.postgres.automatic_memory import enqueue_capture
                        enqueue_capture(database, context, message.id)
                if snapshot["model_center"]:
                    message.run_metadata = {**message.run_metadata, "model_configuration_revision": snapshot["model_center"]["revision"]}
                    if catalog:
                        roles = {**catalog["assignments"], "answer": snapshot["model_center"]["answer_profile_id"] or catalog["assignments"]["answer"]}
                        message.run_metadata = {**message.run_metadata, "model_roles": {role: {
                            "profile_id": key, "profile_version": catalog["profiles"][key]["version"]}
                            for role, key in roles.items()}}
                Repository(database).chat_session(claim.user_id, session_id).updated_at = utcnow()
                if result.kind == "clarification":
                    store.pause(claim, checkpoint.state, result.answer.content, session=database)
                else:
                    store.save_checkpoint(database, current_run, checkpoint.state)
                    store.complete(database, claim.user_id, claim.run_id, message, claim=claim)
                database.commit()
                return message

    def answer_v2(self, user_id: UUID, session_id: UUID, payload: AgentChatInput, request_id: str, on_answer_event=None, *, run_binding=None):
        if payload.approved_tools or payload.approval_catalog_revision is not None:
            raise AppError("CAPABILITY_DURABLE_RUN_REQUIRED", "工具审批必须使用持久 Run", 503)
        if self.settings.capability_center_enabled:
            raise AppError("CAPABILITY_DURABLE_RUN_REQUIRED", "能力调用必须通过持久 Run 入队执行", 503)
        if payload.skill_name is not None:
            raise AppError("CAPABILITIES_DISABLED", "能力中心尚未启用", 503)
        if payload.model_profile_id is not None and self.model_center is None:
            raise AppError("MODEL_CENTER_DISABLED", "Profile 选择要求启用模型中心", 503)
        if self.agent_workflow is None:
            raise AppError("NOT_IMPLEMENTED", "V2 Agent 尚未配置", 503)
        settings, workflow, options = self.settings, self.agent_workflow, {}
        if self.model_center:
            snapshot = self.model_center.snapshot(payload.model_provider, profile_id=payload.model_profile_id)
            settings = snapshot.settings
            workflow = copy(self.agent_workflow)
            workflow.settings, workflow.models = settings, snapshot.router()
            options["call_observer"] = self.model_center.observer(user_id, session_id, request_id, snapshot.revision)
        public_query, web_enabled, warnings = prepare_public_query(payload, self.settings.web_search_enabled)
        if payload.search_mode == "web" and not self.settings.web_search_enabled and not payload.web_urls:
            raise AppError("WEB_SEARCH_UNAVAILABLE", "网络搜索尚未启用", 503)
        selected = payload.model_provider or settings.default_model_provider
        if selected not in settings.enabled_model_providers:
            raise AppError("MODEL_DISABLED", "该模型已停用", 403)
        started = perf_counter()
        with self.sessions.exclusive(session_id) as probe:
            with transaction_scope(self.database) as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, lock=True, kind="chat")
                if run_binding:
                    run_binding[0].guard(database, user_id, run_binding[1])
                ready_ids, owners, history, summary, indexes, kids = self.scope_and_history(repository, user_id, session, payload, settings)
                user_message = ChatMessage(user_id=user_id, session_id=session_id, role="user", content=payload.question)
                as_transaction(database).save(user_message)
                session.updated_at = utcnow()
                database.commit()
            context = RunContext(
                request_id=request_id, user_id=user_id, session_id=session_id, question=payload.question,
                search_mode=payload.search_mode, allowed_document_ids=tuple(ready_ids), selected_provider=selected,
                public_query=public_query, web_enabled=web_enabled, warnings=warnings,
                web_urls=payload.web_urls,
                source_message_id=user_message.id,
                deadline_monotonic=monotonic() + self.settings.agent_timeout,
                knowledge_base_ids=kids, document_owners=tuple(owners.items()), retrieval_indexes=indexes,
                organization_id=session.organization_id, shared_only=session.organization_id is not None or session.kind == "evaluation",
            )
            result = workflow.run(context, history, summary, on_answer_event=on_answer_event, **options)
            if self.model_center:
                result.run_metadata["model_configuration_revision"] = snapshot.revision
            if run_binding:
                run_binding[2]()  # Flush the last coalesced draft delta before terminal events.
            if probe:
                probe()
            with transaction_scope(self.database) as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, lock=True, kind="chat")
                if indexes:
                    DocumentPublication(self.settings, None).validate_targets(database, indexes, lock=True)
                if session.organization_id != context.organization_id:
                    raise AppError("DOCUMENT_SCOPE_CHANGED", "会话空间与冻结范围不一致", 409)
                if context.knowledge_base_ids:
                    final_owners = self.sharing.scope(user_id, context.knowledge_base_ids, ready_ids, session=database, lock=True,
                        organization_id=context.organization_id, include_owned=not context.shared_only)
                    if final_owners != owners:
                        raise AppError("DOCUMENT_SCOPE_CHANGED", "共享权限或文档范围已变化", 409)
                cited = [UUID(citation["document_id"]) for citation in result.citations if citation["type"] == "knowledge"]
                if cited:
                    if context.knowledge_base_ids:
                        if not set(cited).issubset(final_owners):
                            raise AppError("DOCUMENT_SCOPE_CHANGED", "共享文档授权已变化", 409)
                        current = cited
                    else:
                        current = repository.ready_document_ids(user_id, cited, self.settings.embedding_model, require_index=self.settings.document_publication_enabled)
                    if not set(current).issubset(set(ready_ids)):
                        raise AppError("DOCUMENT_SCOPE_CHANGED", "回答期间引用文档已被删除或变更，请重新提问", 409)
                    if self.settings.document_versions_enabled:
                        versions = {}
                        for owner in {owners.get(did, user_id) for did in cited}:
                            versions.update(repository.version_sources(owner, [did for did in cited if owners.get(did, user_id) == owner]))
                        for citation in result.citations:
                            if citation["type"] == "knowledge":
                                citation["document_version"] = versions[str(citation["document_id"])]
                message = ChatMessage(
                    user_id=user_id, session_id=session_id, role="assistant", content=result.answer.content,
                    model_provider=result.answer.provider, model_name=result.answer.model,
                    citations=result.citations, token_usage=result.answer.token_usage or None,
                    run_metadata=result.run_metadata,
                )
                as_transaction(database).save(message)
                session.updated_at = utcnow()
                if self.settings.openviking_enabled:
                    from app.service.automatic_memory import AutomaticMemoryService
                    from app.dao.postgres.automatic_memory import enqueue_capture
                    AutomaticMemoryService(self.settings, self.database, None).validate_citations(database, user_id, result.citations)
                    database.flush()
                    if result.kind != "clarification":
                        enqueue_capture(database, context, message.id)
                if run_binding:
                    database.flush()
                    run_binding[0].complete(database, user_id, run_binding[1], message)
                database.commit()
                logger.info("agent_chat_completed", extra={"resource_id": str(session_id), "model_provider": result.answer.provider, "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
                return message

    def _model_details(self, provider: str | None, profile_id=None) -> tuple[str, str, int]:
        if profile_id is not None and self.model_center is None:
            raise AppError("MODEL_CENTER_DISABLED", "Profile 选择要求启用模型中心", 503)
        snapshot = self.model_center.snapshot(provider, profile_id=profile_id) if self.model_center else None
        settings = snapshot.settings if snapshot else self.settings
        selected = provider or settings.default_model_provider
        names = {"deepseek": settings.deepseek_model, "ollama": settings.ollama_model}
        windows = {"deepseek": settings.deepseek_context_window, "ollama": settings.ollama_context_window}
        if selected not in names:
            raise AppError("MODEL_UNAVAILABLE", "所选模型不可用", 503)
        if selected not in settings.enabled_model_providers:
            raise AppError("MODEL_DISABLED", "该模型已停用", 403)
        return selected, names[selected], snapshot.router().input_budget if snapshot and snapshot.catalog else min(settings.agent_context_token_budget, windows[selected] - settings.model_max_tokens)

    @staticmethod
    def _context_version(session, messages) -> str:
        payload = json.dumps({
            "summary": session.summary, "summarized_through": str(session.summarized_through or ""),
            "last_message": str(messages[-1].id) if messages else "",
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def estimate_context(self, user_id: UUID, payload) -> dict:
        provider, model_name, budget = self._model_details(payload.model_provider, getattr(payload, "model_profile_id", None))
        summary, history, version, ready = "", [], None, []
        if payload.session_id:
            with transaction_scope(self.database) as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, payload.session_id, kind="chat")
                if payload.organization_id is not None and payload.organization_id != session.organization_id:
                    raise AppError("INVALID_WORKSPACE_SCOPE", "上下文估算的空间与会话不一致", 422)
                messages = repository.context_messages(user_id, payload.session_id)
                summary = session.summary
                history = [ChatTurn(item.role, item.content) for item in messages[-self.settings.memory_recent_messages:]]
                version = self._context_version(session, messages)
                if payload.search_mode in {"knowledge", "auto"}:
                    ready = self.document_scope(repository, user_id, session, payload, self.settings)[0]
        else:
            ready = []
            if payload.search_mode in {"knowledge", "auto"} or payload.organization_id is not None:
                with transaction_scope(self.database) as database:
                    from types import SimpleNamespace
                    if payload.organization_id is not None and not self.settings.workspaces_enabled:
                        raise AppError("WORKSPACES_DISABLED", "团队空间尚未启用", 503)
                    if payload.organization_id is not None:
                        Repository(database).workspace_member(user_id, payload.organization_id)
                    context_session = SimpleNamespace(organization_id=payload.organization_id, kind="chat")
                    if payload.search_mode in {"knowledge", "auto"}:
                        ready = self.document_scope(Repository(database), user_id, context_session, payload, self.settings)[0]
        question, name = payload.question, getattr(payload, "skill_name", None)
        instructions, skill_tokens, skill_version, skill_has_tools = "", 0, None, False
        if self.settings.capability_center_enabled:
            question, name = self.capabilities.activation(question, name, allow_empty=True)
            if name is not None:
                from app.entity.bo.capabilities import skill_prompt
                item = self.capabilities.describe_skill(user_id, name)
                instructions = skill_prompt(name, item["version"], item["tool_names"], item["instructions"])
                skill_tokens, skill_version, skill_has_tools = count_tokens(instructions), item["version"], bool(item["tool_names"])
        elif name is not None:
            raise AppError("CAPABILITIES_DISABLED", "能力中心尚未启用", 503)
        has_evidence = skill_has_tools or bool(ready) or payload.search_mode in {"web", "auto"} and (self.settings.web_search_enabled or bool(payload.web_urls)) or payload.search_mode in {"sql", "auto"} and bool(self.settings.sql_allowed_tables)
        reserved = evidence_token_budget(budget) if has_evidence else 0
        estimated = count_tokens(
            AGENT_SYSTEM_PROMPT, summary,
            *(turn.content for turn in history), question,
        ) + reserved + skill_tokens
        ratio = estimated / budget
        recommendation = "over_budget" if ratio > 1 else "urgent_compaction" if ratio >= .9 else "suggest_compaction" if ratio >= .75 else "none"
        if skill_tokens >= budget:
            recommendation = "skill_over_budget"
        return {
            "session_id": payload.session_id, "context_version": version, "provider": provider,
            "model_name": model_name, "estimated_input_tokens": estimated,
            "reserved_evidence_tokens": reserved, "input_budget_tokens": budget,
            "skill_input_tokens": skill_tokens, "skill_name": name, "skill_version": skill_version,
            "usage_ratio": round(ratio, 6), "measurement": "estimated",
            "estimate_basis": "cl100k_base_with_evidence_reserve",
            "thresholds": {"suggest": .75, "urgent": .9}, "recommendation": recommendation,
        }

    def compact(self, user_id: UUID, session_id: UUID, expected_version: str, provider: str | None, profile_id=None):
        if profile_id is not None and self.model_center is None:
            raise AppError("MODEL_CENTER_DISABLED", "Profile 选择要求启用模型中心", 503)
        with self.sessions.exclusive(session_id):
            with transaction_scope(self.database) as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, kind="chat")
                messages = repository.context_messages(user_id, session_id)
                if self._context_version(session, messages) != expected_version:
                    raise AppError("CONTEXT_CHANGED", "会话内容已变化，请刷新后重试", 409)
                cutoff = max(0, len(messages) - 4)
                prefix, index = [], 0
                while index + 1 < cutoff and messages[index].role == "user" and messages[index + 1].role == "assistant":
                    prefix.extend(messages[index:index + 2])
                    index += 2
                if not prefix:
                    raise AppError("NOTHING_TO_COMPACT", "没有可安全压缩的更早问答", 409)
                old_summary = session.summary
                boundary = prefix[-1].id
            source = [{"role": item.role, "content": item.content} for item in prefix]
            models, options, budget = self.agent_workflow.models, {}, self.settings.agent_context_token_budget
            if self.model_center:
                snapshot = self.model_center.snapshot(provider, profile_id=profile_id)
                models = snapshot.router()
                budget = models.input_budget if snapshot.catalog else snapshot.settings.agent_context_token_budget
                options["call_observer"] = self.model_center.observer(user_id, session_id, uuid4().hex, snapshot.revision, "compaction")
            prompts = [
                ChatTurn("system", "将对话整理为忠实、简洁的中文摘要。保留用户目标、约束、决定和未解决问题，不添加事实，不输出解释。"),
                ChatTurn("user", json.dumps({"previous_summary": old_summary, "conversation": source}, ensure_ascii=False)),
            ]
            if count_tokens(*(message.content for message in prompts)) > budget:
                raise AppError("COMPACTION_LIMIT_EXCEEDED", "待压缩历史过长，请新建会话", 409)
            answer = models.generate(prompts, provider, **options)
            with transaction_scope(self.database) as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, lock=True, kind="chat")
                current = repository.context_messages(user_id, session_id)
                if self._context_version(session, current) != expected_version:
                    raise AppError("CONTEXT_CHANGED", "会话内容已变化，请刷新后重试", 409)
                session.summary = answer.content[:16000]
                session.summarized_through = boundary
                session.updated_at = utcnow()
                database.commit()
                remaining = repository.context_messages(user_id, session_id)
                return {
                    "session_id": session_id, "context_version": self._context_version(session, remaining),
                    "summarized_through": boundary, "summary_updated": True,
                }
