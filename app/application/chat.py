import logging
import re
import hashlib
import json
from time import monotonic, perf_counter
from uuid import UUID, uuid4
from copy import copy

from app.application.sessions import SessionService
from app.application.sharing import SharingService
from app.application.model_center import ModelCenter
from app.application.evidence import count_tokens, evidence_token_budget
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput, ChatTurn, RunContext
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatMessage, utcnow
from app.infrastructure.persistence.repository import Repository
from app.workflows.agent import SYSTEM_PROMPT as AGENT_SYSTEM_PROMPT

logger = logging.getLogger("rag")

_SENSITIVE_PUBLIC_QUERY = re.compile(
    r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S{8,}"
)


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
    def __init__(self, settings: Settings, database: Database, sessions: SessionService, agent_workflow):
        self.settings = settings
        self.database = database
        self.sessions = sessions
        self.agent_workflow = agent_workflow
        self.sharing = SharingService(settings, database)
        self.model_center = ModelCenter(settings, database) if settings.model_center_enabled else None

    def answer_v2(self, user_id: UUID, session_id: UUID, payload: AgentChatInput, request_id: str, on_answer_event=None, *, run_binding=None):
        if self.agent_workflow is None:
            raise AppError("NOT_IMPLEMENTED", "V2 Agent 尚未配置", 503)
        settings, workflow, options = self.settings, self.agent_workflow, {}
        if self.model_center:
            snapshot = self.model_center.snapshot(payload.model_provider)
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
            with self.database.sessions() as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, lock=True)
                if run_binding:
                    run_binding[0].guard(database, user_id, run_binding[1])
                requested = list(payload.document_ids) if payload.document_ids is not None else None
                version_options = {"current_only": True} if self.settings.document_versions_enabled else {}
                ready_ids = [] if payload.search_mode in {"web", "sql"} or payload.knowledge_base_ids else repository.ready_document_ids(user_id, requested, self.settings.embedding_model, **version_options)
                owners = {}
                if payload.knowledge_base_ids:
                    if payload.search_mode in {"web", "sql"}:
                        raise AppError("INVALID_SCOPE", "此模式不接受共享知识库", 422)
                    owners = self.sharing.scope(user_id, payload.knowledge_base_ids, requested, session=database)
                    ready_ids = list(owners)
                history = [ChatTurn(message.role, message.content) for message in repository.context_messages(
                    user_id, session_id
                )[-self.settings.memory_recent_messages:]]
                summary = session.summary
                user_message = ChatMessage(user_id=user_id, session_id=session_id, role="user", content=payload.question)
                database.add(user_message)
                session.updated_at = utcnow()
                database.commit()
            context = RunContext(
                request_id=request_id, user_id=user_id, session_id=session_id, question=payload.question,
                search_mode=payload.search_mode, allowed_document_ids=tuple(ready_ids), selected_provider=selected,
                public_query=public_query, web_enabled=web_enabled, warnings=warnings,
                web_urls=payload.web_urls,
                source_message_id=user_message.id,
                deadline_monotonic=monotonic() + self.settings.agent_timeout,
                knowledge_base_ids=payload.knowledge_base_ids, document_owners=tuple(owners.items()),
            )
            result = workflow.run(context, history, summary, on_answer_event=on_answer_event, **options)
            if self.model_center:
                result.run_metadata["model_configuration_revision"] = snapshot.revision
            if run_binding:
                run_binding[2]()  # Flush the last coalesced draft delta before terminal events.
            if probe:
                probe()
            with self.database.sessions() as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, lock=True)
                if payload.knowledge_base_ids:
                    final_owners = self.sharing.scope(user_id, payload.knowledge_base_ids, ready_ids, session=database, lock=True)
                    if final_owners != owners:
                        raise AppError("DOCUMENT_SCOPE_CHANGED", "共享权限或文档范围已变化", 409)
                cited = [UUID(citation["document_id"]) for citation in result.citations if citation["type"] == "knowledge"]
                if cited:
                    if payload.knowledge_base_ids:
                        if not set(cited).issubset(final_owners):
                            raise AppError("DOCUMENT_SCOPE_CHANGED", "共享文档授权已变化", 409)
                        current = cited
                    else:
                        current = repository.ready_document_ids(user_id, cited, self.settings.embedding_model)
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
                database.add(message)
                session.updated_at = utcnow()
                if run_binding:
                    database.flush()
                    run_binding[0].complete(database, user_id, run_binding[1], message)
                database.commit()
                logger.info("agent_chat_completed", extra={"resource_id": str(session_id), "model_provider": result.answer.provider, "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
                return message

    def _model_details(self, provider: str | None) -> tuple[str, str, int]:
        settings = self.model_center.snapshot(provider).settings if self.model_center else self.settings
        selected = provider or settings.default_model_provider
        names = {"deepseek": settings.deepseek_model, "ollama": settings.ollama_model}
        windows = {"deepseek": settings.deepseek_context_window, "ollama": settings.ollama_context_window}
        if selected not in names:
            raise AppError("MODEL_UNAVAILABLE", "所选模型不可用", 503)
        if selected not in settings.enabled_model_providers:
            raise AppError("MODEL_DISABLED", "该模型已停用", 403)
        return selected, names[selected], min(settings.agent_context_token_budget, windows[selected] - settings.model_max_tokens)

    @staticmethod
    def _context_version(session, messages) -> str:
        payload = json.dumps({
            "summary": session.summary, "summarized_through": str(session.summarized_through or ""),
            "last_message": str(messages[-1].id) if messages else "",
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def estimate_context(self, user_id: UUID, payload) -> dict:
        provider, model_name, budget = self._model_details(payload.model_provider)
        summary, history, version, ready = "", [], None, []
        if payload.session_id:
            with self.database.sessions() as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, payload.session_id)
                messages = repository.context_messages(user_id, payload.session_id)
                summary = session.summary
                history = [ChatTurn(item.role, item.content) for item in messages[-self.settings.memory_recent_messages:]]
                version = self._context_version(session, messages)
                if payload.search_mode in {"knowledge", "auto"}:
                    requested = list(payload.document_ids) if payload.document_ids else None
                    ready = list(self.sharing.scope(user_id, payload.knowledge_base_ids, requested, session=database)) if payload.knowledge_base_ids else repository.ready_document_ids(user_id, requested, self.settings.embedding_model, current_only=True if self.settings.document_versions_enabled else None)
        else:
            ready = []
            if payload.search_mode in {"knowledge", "auto"}:
                with self.database.sessions() as database:
                    requested = list(payload.document_ids) if payload.document_ids else None
                    ready = list(self.sharing.scope(user_id, payload.knowledge_base_ids, requested, session=database)) if payload.knowledge_base_ids else Repository(database).ready_document_ids(user_id, requested, self.settings.embedding_model, current_only=True if self.settings.document_versions_enabled else None)
        has_evidence = bool(ready) or payload.search_mode in {"web", "auto"} and (self.settings.web_search_enabled or bool(payload.web_urls)) or payload.search_mode in {"sql", "auto"} and bool(self.settings.sql_allowed_tables)
        reserved = evidence_token_budget(budget) if has_evidence else 0
        estimated = count_tokens(
            AGENT_SYSTEM_PROMPT, summary,
            *(turn.content for turn in history), payload.question,
        ) + reserved
        ratio = estimated / budget
        recommendation = "over_budget" if ratio > 1 else "urgent_compaction" if ratio >= .9 else "suggest_compaction" if ratio >= .75 else "none"
        return {
            "session_id": payload.session_id, "context_version": version, "provider": provider,
            "model_name": model_name, "estimated_input_tokens": estimated,
            "reserved_evidence_tokens": reserved, "input_budget_tokens": budget,
            "usage_ratio": round(ratio, 6), "measurement": "estimated",
            "estimate_basis": "cl100k_base_with_evidence_reserve",
            "thresholds": {"suggest": .75, "urgent": .9}, "recommendation": recommendation,
        }

    def compact(self, user_id: UUID, session_id: UUID, expected_version: str, provider: str | None):
        with self.sessions.exclusive(session_id):
            with self.database.sessions() as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id)
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
                snapshot = self.model_center.snapshot(provider)
                models = snapshot.router()
                budget = snapshot.settings.agent_context_token_budget
                options["call_observer"] = self.model_center.observer(user_id, session_id, uuid4().hex, snapshot.revision, "compaction")
            prompts = [
                ChatTurn("system", "将对话整理为忠实、简洁的中文摘要。保留用户目标、约束、决定和未解决问题，不添加事实，不输出解释。"),
                ChatTurn("user", json.dumps({"previous_summary": old_summary, "conversation": source}, ensure_ascii=False)),
            ]
            if count_tokens(*(message.content for message in prompts)) > budget:
                raise AppError("COMPACTION_LIMIT_EXCEEDED", "待压缩历史过长，请新建会话", 409)
            answer = models.generate(prompts, provider, **options)
            with self.database.sessions() as database:
                repository = Repository(database)
                session = repository.chat_session(user_id, session_id, lock=True)
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
