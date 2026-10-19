from dataclasses import dataclass
from contextlib import nullcontext
from datetime import timedelta
from time import perf_counter
from typing import Literal
from uuid import uuid4
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, inspect, select

from app.core.errors import AppError
from app.domain.model_catalog import ModelCatalog, RegisteredModelProfile, MODEL_ROLES
from app.infrastructure.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
from app.infrastructure.persistence.models import ModelCall, ModelConfiguration, ModelConfigurationVersion, User, utcnow
from app.infrastructure.persistence.repository import Repository

Provider = Literal["deepseek", "ollama"]


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model_name: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9._:/-]+$")
    context_window: int = Field(ge=4096, le=2_000_000)
    enabled: bool


class ManagedModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: dict[Provider, ModelProfile]
    default_provider: Provider
    allow_fallback: bool
    timeout_seconds: float = Field(gt=0, le=120)
    max_output_tokens: int = Field(ge=1, le=16384)

    @model_validator(mode="after")
    def valid(self):
        if set(self.profiles) != {"deepseek", "ollama"}:
            raise ValueError("Both existing provider profiles are required")
        if not self.profiles[self.default_provider].enabled:
            raise ValueError("Default provider must be enabled")
        if self.allow_fallback and not all(profile.enabled for profile in self.profiles.values()):
            raise ValueError("Fallback requires both profiles enabled")
        if any(profile.context_window < self.max_output_tokens + 1024 for profile in self.profiles.values()):
            raise ValueError("Context window must reserve input space beyond output tokens")
        return self


@dataclass(frozen=True)
class ModelSnapshot:
    settings: object
    revision: int
    configuration: ManagedModels
    health: dict
    catalog: ModelCatalog | None = None
    answer_profile_id: str | None = None

    def router(self):
        if self.catalog is not None:
            from app.infrastructure.llms.catalog import CatalogRouter
            return CatalogRouter(self.settings, self.catalog, profile_id=self.answer_profile_id)
        return ModelRouter(self.settings, {"deepseek": DeepSeekProvider(self.settings), "ollama": OllamaProvider(self.settings)})


def catalog_from_legacy(configuration):
    old = ManagedModels.model_validate(configuration)
    profiles = {provider: RegisteredModelProfile(id=provider, version=1, display_name=provider,
        provider=provider, connection_id=provider, model_name=profile.model_name, context_window=profile.context_window,
        max_output_tokens=old.max_output_tokens, timeout_seconds=old.timeout_seconds, supports_stream=True,
        supports_tools=True, supports_structured_output=True, enabled=profile.enabled,
        fallback_profile_id="ollama" if provider == "deepseek" and old.allow_fallback else None)
        for provider, profile in old.profiles.items()}
    return ModelCatalog(profiles=profiles, assignments={role: old.default_provider for role in MODEL_ROLES},
                        provider_defaults={key: key for key, profile in old.profiles.items() if profile.enabled})


def legacy_projection(catalog, settings):
    answer = catalog.require("answer")
    profiles = {}
    for provider in ("deepseek", "ollama"):
        key = catalog.provider_defaults.get(provider)
        profile = catalog.profiles.get(key) if key else answer if provider == answer.provider else None
        profiles[provider] = ModelProfile(model_name=profile.model_name if profile else getattr(settings, provider + "_model"),
            context_window=profile.context_window if profile else getattr(settings, provider + "_context_window"), enabled=profile is not None)
    # Old clients see their provider slots. They cannot express same-provider
    # fallback or role assignments and cannot overwrite a v2 catalog.
    fallback = catalog.profiles.get(answer.fallback_profile_id)
    return ManagedModels(profiles=profiles, default_provider=answer.provider,
        allow_fallback=answer.provider == "deepseek" and fallback is not None and fallback.provider == "ollama"
                       and all(profile.enabled for profile in profiles.values()),
        timeout_seconds=answer.timeout_seconds, max_output_tokens=min(answer.max_output_tokens,
            *(profile.context_window - 1024 for profile in profiles.values())))


class ModelCenter:
    def __init__(self, settings, database):
        self.settings, self.database = settings, database

    def require(self):
        if not self.settings.model_center_enabled:
            raise AppError("MODEL_CENTER_DISABLED", "模型中心尚未启用", 503)

    def is_admin(self, uid):
        from app.application.permissions import is_platform_admin
        return is_platform_admin(self.database, uid)

    def admin(self, uid):
        from app.application.permissions import require_platform_admin
        self.require()
        require_platform_admin(self.database, uid)

    def initialize(self):
        """Explicit bootstrap only: never seed or overwrite configuration on startup."""
        with self.database.sessions() as session:
            if not inspect(session.connection()).has_table("model_configuration_versions", schema="rag"):
                raise AppError("MODEL_CENTER_MIGRATION_REQUIRED", "请先执行 python -m app.migrate 保存模型配置历史表", 503)
            existing = session.get(ModelConfiguration, 1)
            if existing:
                if session.get(ModelConfigurationVersion, existing.revision) is None:
                    session.add(ModelConfigurationVersion(revision=existing.revision, configuration=dict(existing.configuration), created_at=existing.updated_at))
                    session.commit()
                return
            enabled = {name: name in self.settings.enabled_model_providers for name in ("deepseek", "ollama")}
            enabled["deepseek"] &= bool(self.settings.deepseek_api_key.get_secret_value())
            default = self.settings.default_model_provider
            if not enabled[default]:
                default = next((key for key, value in enabled.items() if value), None)
            if default is None:
                raise AppError("CONFIGURATION_ERROR", "至少配置一个可启用的模型", 503)
            config = ManagedModels(profiles={
                "deepseek": ModelProfile(model_name=self.settings.deepseek_model, context_window=self.settings.deepseek_context_window, enabled=enabled["deepseek"]),
                "ollama": ModelProfile(model_name=self.settings.ollama_model, context_window=self.settings.ollama_context_window, enabled=enabled["ollama"]),
            }, default_provider=default, allow_fallback=self.settings.allow_model_fallback and all(enabled.values()),
                timeout_seconds=min(120, self.settings.model_timeout), max_output_tokens=self.settings.model_max_tokens)
            session.add(ModelConfiguration(id=1, revision=1, configuration=config.model_dump(mode="json"), health={}))
            session.add(ModelConfigurationVersion(revision=1, configuration=config.model_dump(mode="json")))
            session.commit()

    def row(self, session, *, lock=False):
        query = select(ModelConfiguration).where(ModelConfiguration.id == 1)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise AppError("MODEL_CENTER_NOT_INITIALIZED", "请显式执行 bootstrap --model-center", 503)
        return row

    def migrate_retired_deepseek_chat(self):
        """Explicit operator maintenance; preserve roles, revisions and frozen runs."""
        if urlsplit(self.settings.deepseek_base_url).hostname != "api.deepseek.com":
            raise AppError("MODEL_MIGRATION_CUSTOM_ENDPOINT", "自定义端点请通过模型中心确认可用模型后修改", 409)
        with self.database.sessions() as session:
            row = self.row(session, lock=True)
            configuration = dict(row.configuration)
            canonical = configuration.get("schema_version") == 2
            parsed = ModelCatalog.model_validate(configuration) if canonical else ManagedModels.model_validate(configuration)
            migrated = 0
            profiles = {}
            for key, profile in parsed.profiles.items():
                provider = profile.provider if canonical else key
                if provider == "deepseek" and profile.model_name == "deepseek-chat":
                    updates = {"model_name": "deepseek-flash"}
                    if canonical:
                        updates["version"] = profile.version + 1
                    profile = profile.model_copy(update=updates)
                    migrated += 1
                profiles[key] = profile
            if migrated:
                saved = parsed.model_copy(update={"profiles": profiles}).model_dump(mode="json")
                row.configuration = saved
                row.revision += 1
                row.updated_at = utcnow()
                row.health = {}
                session.add(ModelConfigurationVersion(revision=row.revision, configuration=saved))
                session.commit()
            return {"revision": row.revision, "migrated_profiles": migrated}

    def snapshot(self, provider=None, *, session=None, profile_id=None):
        self.require()
        if provider is not None and profile_id is not None:
            raise AppError("INVALID_MODEL_SELECTION", "模型 Profile 与提供商不能同时指定", 422)
        with (nullcontext(session) if session is not None else self.database.sessions()) as session:
            row = self.row(session)
            catalog = ModelCatalog.model_validate(row.configuration) if row.configuration.get("schema_version") == 2 else None
            if profile_id is not None and catalog is None:
                catalog = catalog_from_legacy(row.configuration)
            config = legacy_projection(catalog, self.settings) if catalog else ManagedModels.model_validate(row.configuration)
            updates = {"default_model_provider": config.default_provider, "allow_model_fallback": config.allow_fallback,
                       "model_timeout": config.timeout_seconds, "model_max_tokens": config.max_output_tokens,
                       "enabled_model_providers": [key for key, profile in config.profiles.items() if profile.enabled]}
            for name, profile in config.profiles.items():
                updates[f"{name}_model"] = profile.model_name
                updates[f"{name}_context_window"] = profile.context_window
            selected = provider or config.default_provider
            if selected not in config.profiles or not config.profiles[selected].enabled:
                raise AppError("MODEL_DISABLED", "该模型已停用", 403)
            budget = min(self.settings.agent_context_token_budget, config.profiles[selected].context_window - config.max_output_tokens)
            if selected == "deepseek" and config.allow_fallback:
                budget = min(budget, config.profiles["ollama"].context_window - config.max_output_tokens)
            updates["agent_context_token_budget"] = budget
            selected_id = None
            if catalog:
                selected_id = profile_id or (catalog.provider_defaults.get(provider, catalog.assignments["answer"] if provider == catalog.require("answer").provider else None) if provider else catalog.assignments["answer"])
                profile = catalog.profiles.get(selected_id)
                if profile is None or not profile.enabled or not profile.supports("answer"):
                    raise AppError("MODEL_DISABLED", "所选 Profile 未启用或不具备回答所需能力", 403)
            if catalog and selected_id:
                selected_profile = catalog.profiles[selected_id]
                updates[f"{selected_profile.provider}_model"] = selected_profile.model_name
                updates[f"{selected_profile.provider}_context_window"] = selected_profile.context_window
                updates.update(default_model_provider=selected_profile.provider, model_timeout=selected_profile.timeout_seconds,
                               model_max_tokens=selected_profile.max_output_tokens)
                updates["enabled_model_providers"] = list(dict.fromkeys([*updates["enabled_model_providers"], selected_profile.provider]))
                # Per-role routers apply their own window/output limits. The
                # evaluator and planner must not shrink the answer model's cap.
                updates["agent_context_token_budget"] = self.settings.agent_context_token_budget
            return ModelSnapshot(self.settings.model_copy(update=updates), row.revision, config, dict(row.health or {}), catalog, selected_id)

    def profiles(self, snapshot):
        result = []
        for name, profile in snapshot.configuration.profiles.items():
            configured = name == "ollama" or bool(self.settings.deepseek_api_key.get_secret_value())
            health_key = snapshot.catalog.provider_defaults.get(name, snapshot.catalog.assignments["answer"] if snapshot.catalog.require("answer").provider == name else name) if snapshot.catalog else name
            health = snapshot.health.get(health_key, {})
            checked = health.get("checked_at")
            recent = False
            if checked:
                from datetime import datetime
                recent = (utcnow() - datetime.fromisoformat(checked)).total_seconds() < 300
            state = health.get("state", "unknown") if recent and health.get("revision") == snapshot.revision else "unknown"
            if not profile.enabled:
                state = "disabled"
            elif not configured:
                state = "not_configured"
            result.append({"provider": name, "model_name": profile.model_name, "enabled": profile.enabled,
                           "available": profile.enabled and configured, "health": state,
                           "checked_at": checked, "error_code": health.get("error_code") if state == "unavailable" else None,
                           "input_budget_tokens": min(self.settings.agent_context_token_budget, profile.context_window - snapshot.configuration.max_output_tokens,
                                snapshot.configuration.profiles["ollama"].context_window - snapshot.configuration.max_output_tokens if name == "deepseek" and snapshot.configuration.allow_fallback else self.settings.agent_context_token_budget),
                           "cache_usage_supported": name == "deepseek"})
        return result

    def view(self, uid):
        snapshot = self.snapshot()
        return {"revision": snapshot.revision, "configuration": snapshot.configuration.model_dump(mode="json"),
                "models": self.profiles(snapshot), "can_manage": self.is_admin(uid), "health_ttl_seconds": 300}

    def update(self, uid, revision, config):
        self.admin(uid)
        config = ManagedModels.model_validate(config.model_dump())
        if config.profiles["deepseek"].enabled and not self.settings.deepseek_api_key.get_secret_value():
            raise AppError("MODEL_CREDENTIALS_MISSING", "请先在服务端配置 DeepSeek 凭据", 422)
        with self.database.sessions() as session:
            row = self.row(session, lock=True)
            if row.revision != revision:
                raise AppError("MODEL_CONFIGURATION_CHANGED", "模型配置已更新，请刷新后重试", 409)
            if row.configuration.get("schema_version") == 2:
                raise AppError("MODEL_CATALOG_REQUIRED", "请通过模型目录接口管理 Profile 与角色，旧接口无法表达完整配置", 409)
            row.configuration = config.model_dump(mode="json")
            row.revision += 1
            row.health = {}
            session.add(ModelConfigurationVersion(revision=row.revision, configuration=config.model_dump(mode="json"), created_by=uid))
            session.commit()
        return self.view(uid)

    def catalog_view(self, uid):
        self.require()
        with self.database.sessions() as session:
            row = self.row(session)
            catalog = ModelCatalog.model_validate(row.configuration) if row.configuration.get("schema_version") == 2 else catalog_from_legacy(row.configuration)
            return {"revision": row.revision, "catalog": catalog.model_dump(mode="json"), "can_manage": self.is_admin(uid),
                    "models": self.catalog_models(catalog, row.revision, row.health or {}),
                    "connections": [{"id": provider, "provider": provider,
                                     "configured": provider == "ollama" or bool(self.settings.deepseek_api_key.get_secret_value())}
                                    for provider in ("deepseek", "ollama")]}

    def catalog_models(self, catalog, revision, health):
        from datetime import datetime
        result = []
        for key, profile in catalog.profiles.items():
            configured = profile.provider == "ollama" or bool(self.settings.deepseek_api_key.get_secret_value())
            record = health.get(key, {})
            checked = record.get("checked_at")
            try:
                fresh = checked and (utcnow() - datetime.fromisoformat(checked)).total_seconds() < 300
            except (TypeError, ValueError):
                fresh = False
            state = record.get("state", "unknown") if fresh and record.get("revision") == revision else "unknown"
            if not profile.enabled:
                state = "disabled"
            elif not configured:
                state = "not_configured"
            limits = [profile] + ([catalog.profiles[profile.fallback_profile_id]] if profile.fallback_profile_id else [])
            result.append({"profile_id": key, "profile_version": profile.version, "display_name": profile.display_name,
                "provider": profile.provider, "model_name": profile.model_name, "enabled": profile.enabled,
                "available": profile.enabled and configured and profile.supports("answer"),
                "health": state, "checked_at": checked, "error_code": record.get("error_code") if state == "unavailable" else None,
                "input_budget_tokens": min(self.settings.agent_context_token_budget, *(item.context_window - item.max_output_tokens for item in limits)),
                "cache_usage_supported": profile.provider == "deepseek"})
        return result

    def selectable_profiles(self, snapshot):
        catalog = snapshot.catalog or catalog_from_legacy(snapshot.configuration.model_dump(mode="json"))
        return self.catalog_models(catalog, snapshot.revision, snapshot.health), catalog.assignments["answer"]

    def update_catalog(self, uid, revision, catalog):
        self.admin(uid)
        catalog = ModelCatalog.model_validate(catalog.model_dump())
        for profile in catalog.profiles.values():
            if profile.connection_id != profile.provider:
                raise AppError("MODEL_CONNECTION_UNAVAILABLE", "Profile 必须使用与提供商匹配的服务端连接", 422)
            if profile.enabled and profile.provider == "deepseek" and not self.settings.deepseek_api_key.get_secret_value():
                raise AppError("MODEL_CREDENTIALS_MISSING", "请先在服务端配置 DeepSeek 凭据", 422)
        with self.database.sessions() as session:
            row = self.row(session, lock=True)
            if row.revision != revision:
                raise AppError("MODEL_CONFIGURATION_CHANGED", "模型配置已更新，请刷新后重试", 409)
            old = ModelCatalog.model_validate(row.configuration) if row.configuration.get("schema_version") == 2 else catalog_from_legacy(row.configuration)
            profiles = {}
            for key, profile in catalog.profiles.items():
                previous = old.profiles.get(key)
                if previous is None and session.scalar(select(ModelConfigurationVersion.revision).where(
                        ModelConfigurationVersion.configuration["profiles"][key].as_string().is_not(None)).limit(1)) is not None:
                    raise AppError("MODEL_PROFILE_RETIRED", "已删除的 Profile 标识不能重用，请创建新的标识", 409)
                if profile.version != (previous.version if previous else 1):
                    raise AppError("MODEL_PROFILE_VERSION_CHANGED", "Profile 版本与当前目录不一致", 409)
                changed = previous and profile.model_dump(exclude={"version"}) != previous.model_dump(exclude={"version"})
                profiles[key] = profile.model_copy(update={"version": profile.version + bool(changed)})
            saved = ModelCatalog.model_validate({**catalog.model_dump(), "profiles": profiles})
            row.configuration, row.health, row.revision = saved.model_dump(mode="json"), {}, row.revision + 1
            session.add(ModelConfigurationVersion(revision=row.revision, configuration=saved.model_dump(mode="json"), created_by=uid))
            session.commit()
        return self.catalog_view(uid)

    def history(self, uid, offset=0, limit=20):
        self.admin(uid)
        with self.database.sessions() as session:
            rows = session.scalars(select(ModelConfigurationVersion).order_by(ModelConfigurationVersion.revision.desc())
                                   .offset(offset).limit(limit)).all()
            return {"items": [{"revision": row.revision, "created_by": row.created_by, "created_at": row.created_at,
                               "configuration": row.configuration} for row in rows],
                    "total": session.scalar(select(func.count()).select_from(ModelConfigurationVersion))}

    def probe(self, uid, provider, *, transport=None):
        self.admin(uid)
        snapshot = self.snapshot()
        if snapshot.catalog:
            key = snapshot.catalog.provider_defaults.get(provider)
            if key is None and snapshot.catalog.require("answer").provider == provider:
                key = snapshot.catalog.assignments["answer"]
            if key is None:
                raise AppError("MODEL_DISABLED", "该提供商没有默认回答 Profile", 409)
            self.probe_profile(uid, key, transport=transport)
            return self.view(uid)
        profile = snapshot.configuration.profiles[provider]
        if not profile.enabled:
            raise AppError("MODEL_DISABLED", "该模型已停用", 409)
        state, code = self._probe_directory(provider, profile.model_name, transport)
        self._save_probe(snapshot.revision, provider, state, code)
        return self.view(uid)

    def probe_profile(self, uid, profile_id, *, transport=None):
        self.admin(uid)
        with self.database.sessions() as session:
            row = self.row(session)
            revision = row.revision
            catalog = ModelCatalog.model_validate(row.configuration) if row.configuration.get("schema_version") == 2 else catalog_from_legacy(row.configuration)
            profile = catalog.profiles.get(profile_id)
            if profile is None:
                raise AppError("NOT_FOUND", "模型 Profile 不存在", 404)
            if not profile.enabled:
                raise AppError("MODEL_DISABLED", "模型 Profile 已停用", 409)
            if profile.connection_id != profile.provider:
                raise AppError("MODEL_CONNECTION_UNAVAILABLE", "服务端连接未配置", 503)
        state, code = self._probe_directory(profile.provider, profile.model_name, transport)
        self._save_probe(revision, profile_id, state, code)
        return self.catalog_view(uid)

    def _probe_directory(self, provider, model_name, transport):
        state, code = "unavailable", None
        key = self.settings.deepseek_api_key.get_secret_value()
        if provider == "deepseek" and not key:
            raise AppError("MODEL_CREDENTIALS_MISSING", "服务端模型凭据未配置", 409)
        try:
            base = self.settings.deepseek_base_url if provider == "deepseek" else self.settings.ollama_base_url
            with httpx.Client(timeout=5, follow_redirects=False, transport=transport) as client:
                response = client.get(base.rstrip("/") + ("/models" if provider == "deepseek" else "/api/tags"),
                                      headers={"Authorization": f"Bearer {key}"} if provider == "deepseek" else {})
                response.raise_for_status()
                body = response.json()
                entries = body["data"] if provider == "deepseek" else body["models"]
                names = [entry["id"] if provider == "deepseek" else entry["name"] for entry in entries]
                if model_name in names or provider == "ollama" and f"{model_name}:latest" in names:
                    state = "available"
                else:
                    code = "MODEL_NOT_LISTED"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            code = "MODEL_PROBE_FAILED"
        return state, code

    def _save_probe(self, revision, key, state, code):
        with self.database.sessions() as session:
            row = self.row(session, lock=True)
            if row.revision != revision:
                raise AppError("MODEL_CONFIGURATION_CHANGED", "检测期间模型配置已更新，请重新检测", 409)
            row.health = {**(row.health or {}), key: {"revision": revision, "state": state,
                          "checked_at": utcnow().isoformat(), "error_code": code}}
            session.commit()

    def observer(self, uid, sid, request_id, revision, operation="agent"):
        return CallObserver(self.database, uid, sid, request_id, revision, operation)

    def statistics(self, uid, days=30, global_scope=False):
        self.require()
        if global_scope:
            self.admin(uid)
        with self.database.sessions() as session:
            query = select(ModelCall.provider, ModelCall.model_name, ModelCall.model_role, ModelCall.model_profile_id,
                ModelCall.model_profile_version, func.count().label("attempts"),
                func.sum(case((ModelCall.status == "succeeded", 1), else_=0)).label("succeeded"),
                func.sum(case((ModelCall.status == "failed", 1), else_=0)).label("failed"),
                func.sum(case((ModelCall.status == "started", 1), else_=0)).label("incomplete"),
                func.sum(case((ModelCall.fallback.is_(True), 1), else_=0)).label("fallback_attempts"),
                func.count(ModelCall.total_tokens).label("reported_calls"),
                func.sum(ModelCall.input_tokens).label("reported_input_tokens"),
                func.sum(ModelCall.output_tokens).label("reported_output_tokens"),
                func.sum(ModelCall.total_tokens).label("reported_total_tokens"),
                func.avg(ModelCall.elapsed_ms).label("average_ms"),
            ).where(ModelCall.created_at >= utcnow() - timedelta(days=days))
            if not global_scope:
                query = query.where(ModelCall.user_id == uid)
            rows = [dict(row._mapping) for row in session.execute(query.group_by(ModelCall.provider, ModelCall.model_name,
                ModelCall.model_role, ModelCall.model_profile_id, ModelCall.model_profile_version))]
        for row in rows:
            row["total_tokens"] = row["reported_total_tokens"] if row["reported_calls"] == row["attempts"] else None
            row["average_ms"] = round(row["average_ms"], 1) if row["average_ms"] is not None else None
        return {"scope": "all_users" if global_scope else "current_user", "days": days, "models": rows,
                "includes": ["agent", "compaction", "evaluation"], "missing_usage_is_zero": False}


class CallObserver:
    def __init__(self, database, uid, sid, request_id, revision, operation, *, model_role=None, profile_id=None, profile_version=None):
        self.database, self.uid, self.sid = database, uid, sid
        self.request_id, self.revision, self.operation = request_id, revision, operation
        self.model_role, self.profile_id, self.profile_version = model_role, profile_id, profile_version

    def with_profile(self, role, profile_id, version):
        return CallObserver(self.database, self.uid, self.sid, self.request_id, self.revision, self.operation,
                            model_role=role, profile_id=profile_id, profile_version=version)

    def start(self, provider, model_name, attempt_index, fallback=False):
        try:
            with self.database.sessions() as session:
                Repository(session).chat_session(self.uid, self.sid)
                row = ModelCall(id=uuid4(), user_id=self.uid, session_id=self.sid, request_id=self.request_id,
                                operation=self.operation, attempt_index=attempt_index, configuration_revision=self.revision,
                                provider=provider, model_name=model_name, status="started", fallback=fallback,
                                model_role=self.model_role, model_profile_id=self.profile_id, model_profile_version=self.profile_version)
                session.add(row);session.commit()
                return row.id, perf_counter()
        except AppError:
            raise
        except Exception:
            raise AppError("MODEL_METRICS_UNAVAILABLE", "无法保存模型调用记录，本次未继续调用", 503) from None

    def finish(self, handle, message=None, error=None):
        from app.infrastructure.llms.providers import retryable_model_error
        usage = dict(getattr(message, "usage_metadata", None) or getattr(message, "token_usage", None) or {})
        try:
            with self.database.sessions() as session:
                row = session.get(ModelCall, handle[0])
                if row is None or row.status != "started":
                    raise AppError("MODEL_METRICS_UNAVAILABLE", "模型调用记录已变化", 503)
                row.status = "failed" if error else "succeeded"
                row.elapsed_ms = max(0, round((perf_counter() - handle[1]) * 1000))
                for name in ("input_tokens", "output_tokens", "total_tokens"):
                    value = usage.get(name)
                    setattr(row, name, value if type(value) is int and 0 <= value <= 2_147_483_647 else None)
                row.error_code = (error.code if isinstance(error, AppError) else "MODEL_UPSTREAM_UNAVAILABLE" if retryable_model_error(error) else "MODEL_CALL_FAILED") if error else None
                session.commit()
        except Exception:
            raise AppError("MODEL_METRICS_UNAVAILABLE", "调用结果统计未能保存，调用状态需核对", 503) from None
