"""Route a frozen catalog to existing adapters, including same-provider fallback."""
from app.core.errors import AppError, UpstreamError
from app.manager.llms.providers import DeepSeekProvider, OllamaProvider


class CatalogRouter:
    def __init__(self, settings, catalog, *, profile_id=None, role="answer"):
        self.settings, self.catalog, self.role = settings, catalog, role
        self.profile = catalog.profiles[profile_id or catalog.assignments[role]]
        if not self.profile.enabled or not self.profile.supports(role):
            raise AppError("MODEL_DISABLED", "模型未启用或不支持当前角色", 409)
        self.fallback_profile = catalog.profiles.get(self.profile.fallback_profile_id)

    @property
    def input_budget(self):
        profiles = [self.profile] + ([self.fallback_profile] if self.fallback_profile else [])
        return min(self.settings.agent_context_token_budget, *(profile.context_window - profile.max_output_tokens for profile in profiles))

    def for_role(self, role):
        if role == self.role:
            return self
        return CatalogRouter(self.settings, self.catalog, role=role)

    def implementation(self, profile):
        if profile.connection_id != profile.provider:
            raise AppError("MODEL_CONNECTION_UNAVAILABLE", "模型连接未在服务端配置", 503)
        settings = self.settings.model_copy(update={f"{profile.provider}_model": profile.model_name,
            f"{profile.provider}_context_window": profile.context_window, "model_timeout": profile.timeout_seconds,
            "model_max_tokens": profile.max_output_tokens})
        return (DeepSeekProvider if profile.provider == "deepseek" else OllamaProvider)(settings)

    def agent_model(self, provider, timeout_seconds):
        profile = self.profile
        if provider is not None and provider != profile.provider:
            if not self.fallback_profile or self.fallback_profile.provider != provider:
                raise AppError("MODEL_DISABLED", "提供商不属于本角色的冻结配置", 403)
            profile = self.fallback_profile
        return self._client(profile, timeout_seconds)

    def _client(self, profile, timeout_seconds):
        implementation = self.implementation(profile)
        return implementation.agent_client(min(timeout_seconds, profile.timeout_seconds)), profile.provider, profile.model_name

    def agent_fallback(self, provider, timeout_seconds):
        return self.fallback_model(timeout_seconds) if self.fallback_profile else None

    def fallback_model(self, timeout_seconds):
        if self.fallback_profile is None:
            raise AppError("MODEL_UNAVAILABLE", "本角色未配置降级模型", 503)
        return self._client(self.fallback_profile, timeout_seconds)

    def generate(self, messages, provider=None, *, call_observer=None):
        if provider is not None and provider != self.profile.provider:
            raise AppError("MODEL_DISABLED", "提供商不属于本角色的冻结配置", 403)

        def invoke(profile, attempt):
            implementation = self.implementation(profile)
            observer = call_observer.with_profile(self.role, profile.id, profile.version) if call_observer and hasattr(call_observer, "with_profile") else call_observer
            handle = observer.start(profile.provider, profile.model_name, attempt, attempt > 1) if observer else None
            try:
                answer = implementation.generate(messages)
            except Exception as error:
                if observer:
                    observer.finish(handle, error=error)
                raise
            if observer:
                observer.finish(handle, message=answer)
            return answer

        try:
            return invoke(self.profile, 1)
        except UpstreamError as error:
            if self.fallback_profile is None or not error.retryable:
                raise
            return invoke(self.fallback_profile, 2)
