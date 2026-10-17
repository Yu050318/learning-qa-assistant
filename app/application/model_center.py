from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, func, select

from app.core.errors import AppError
from app.infrastructure.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
from app.infrastructure.persistence.models import ModelCall, ModelConfiguration, User, utcnow
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

    def router(self):
        return ModelRouter(self.settings, {"deepseek": DeepSeekProvider(self.settings), "ollama": OllamaProvider(self.settings)})


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
            if session.get(ModelConfiguration, 1):
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
            session.commit()

    def row(self, session, *, lock=False):
        query = select(ModelConfiguration).where(ModelConfiguration.id == 1)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise AppError("MODEL_CENTER_NOT_INITIALIZED", "请显式执行 bootstrap --model-center", 503)
        return row

    def snapshot(self, provider=None):
        self.require()
        with self.database.sessions() as session:
            row = self.row(session)
            config = ManagedModels.model_validate(row.configuration)
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
            return ModelSnapshot(self.settings.model_copy(update=updates), row.revision, config, dict(row.health or {}))

    def profiles(self, snapshot):
        result = []
        for name, profile in snapshot.configuration.profiles.items():
            configured = name == "ollama" or bool(self.settings.deepseek_api_key.get_secret_value())
            health = snapshot.health.get(name, {})
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
            row.configuration = config.model_dump(mode="json")
            row.revision += 1
            row.health = {}
            session.commit()
        return self.view(uid)

    def probe(self, uid, provider, *, transport=None):
        self.admin(uid)
        snapshot = self.snapshot()
        profile = snapshot.configuration.profiles[provider]
        state, code = "unavailable", None
        if not profile.enabled:
            raise AppError("MODEL_DISABLED", "该模型已停用", 409)
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
                if profile.model_name in names or provider == "ollama" and f"{profile.model_name}:latest" in names:
                    state = "available"
                else:
                    code = "MODEL_NOT_LISTED"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            code = "MODEL_PROBE_FAILED"
        with self.database.sessions() as session:
            row = self.row(session, lock=True)
            if row.revision != snapshot.revision:
                raise AppError("MODEL_CONFIGURATION_CHANGED", "检测期间模型配置已更新，请重新检测", 409)
            row.health = {**(row.health or {}), provider: {"revision": snapshot.revision, "state": state,
                          "checked_at": utcnow().isoformat(), "error_code": code}}
            session.commit()
        return self.view(uid)

    def observer(self, uid, sid, request_id, revision, operation="agent"):
        return CallObserver(self.database, uid, sid, request_id, revision, operation)

    def statistics(self, uid, days=30, global_scope=False):
        self.require()
        if global_scope:
            self.admin(uid)
        with self.database.sessions() as session:
            query = select(ModelCall.provider, ModelCall.model_name, func.count().label("attempts"),
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
            rows = [dict(row._mapping) for row in session.execute(query.group_by(ModelCall.provider, ModelCall.model_name))]
        for row in rows:
            row["total_tokens"] = row["reported_total_tokens"] if row["reported_calls"] == row["attempts"] else None
            row["average_ms"] = round(row["average_ms"], 1) if row["average_ms"] is not None else None
        return {"scope": "all_users" if global_scope else "current_user", "days": days, "models": rows,
                "includes": ["agent", "compaction"], "missing_usage_is_zero": False}


class CallObserver:
    def __init__(self, database, uid, sid, request_id, revision, operation):
        self.database, self.uid, self.sid = database, uid, sid
        self.request_id, self.revision, self.operation = request_id, revision, operation

    def start(self, provider, model_name, attempt_index, fallback=False):
        try:
            with self.database.sessions() as session:
                Repository(session).chat_session(self.uid, self.sid)
                row = ModelCall(id=uuid4(), user_id=self.uid, session_id=self.sid, request_id=self.request_id,
                                operation=self.operation, attempt_index=attempt_index, configuration_revision=self.revision,
                                provider=provider, model_name=model_name, status="started", fallback=fallback)
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
