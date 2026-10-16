from collections.abc import Iterator
from functools import cached_property

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import ChatOllama
from ollama import ResponseError
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError
from app.domain.contracts import ChatModelProvider, ChatTurn, ModelAnswer


def retryable_model_error(error: Exception) -> bool:
    if isinstance(error, UpstreamError):
        return error.retryable
    if isinstance(error, (APIConnectionError, APITimeoutError, httpx.TransportError, ConnectionError)):
        return True
    if isinstance(error, (APIStatusError, ResponseError)):
        status = error.status_code or 500
        return status == 429 or status >= 500
    return False


def convert_messages(messages: list[ChatTurn]) -> list:
    types = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [types[message.role](content=message.content) for message in messages]


class LangChainProvider:
    name: str
    model_name: str

    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, messages: list[ChatTurn]) -> ModelAnswer:
        try:
            response = self.client.invoke(convert_messages(messages))
            if not isinstance(response.content, str) or not response.content.strip():
                raise UpstreamError("llm")
            return ModelAnswer(response.content, self.name, self.model_name, dict(response.usage_metadata or {}))
        except (APIConnectionError, APITimeoutError, httpx.TransportError, ConnectionError, APIStatusError, ResponseError) as error:
            raise UpstreamError("llm", retryable=retryable_model_error(error)) from None

    def stream(self, messages: list[ChatTurn]) -> Iterator[str]:
        raise AppError("NOT_IMPLEMENTED", "流式适配器将在下一阶段实现", 501)

    def agent_client(self, timeout_seconds: float):
        raise NotImplementedError


class DeepSeekProvider(LangChainProvider):
    name = "deepseek"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.model_name = settings.deepseek_model

    @cached_property
    def client(self):
        return self.agent_client(self.settings.model_timeout)

    def agent_client(self, timeout_seconds: float):
        if not self.settings.deepseek_api_key.get_secret_value():
            raise AppError("CONFIGURATION_ERROR", "请配置 DEEPSEEK_API_KEY", 503)
        return ChatDeepSeek(
            model=self.model_name, api_key=self.settings.deepseek_api_key,
            api_base=self.settings.deepseek_base_url, temperature=0,
            timeout=timeout_seconds, max_retries=0,
            max_tokens=self.settings.model_max_tokens,
        )


class OllamaProvider(LangChainProvider):
    name = "ollama"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.model_name = settings.ollama_model

    @cached_property
    def client(self):
        return self.agent_client(self.settings.model_timeout)

    def agent_client(self, timeout_seconds: float):
        return ChatOllama(
            model=self.model_name, base_url=self.settings.ollama_base_url,
            temperature=0, num_predict=self.settings.model_max_tokens, num_ctx=self.settings.ollama_context_window,
            client_kwargs={"timeout": timeout_seconds},
        )


class ModelRouter:
    def __init__(self, settings: Settings, providers: dict[str, ChatModelProvider]):
        self.settings = settings
        self.providers = providers

    def generate(self, messages: list[ChatTurn], provider: str | None = None, *, call_observer=None) -> ModelAnswer:
        selected = provider or self.settings.default_model_provider
        implementation = self._provider(selected)

        def invoke(implementation, attempt, fallback=False):
            handle = call_observer.start(implementation.name, implementation.model_name, attempt, fallback) if call_observer else None
            try:
                result = implementation.generate(messages)
            except Exception as error:
                if call_observer:
                    call_observer.finish(handle, error=error)
                raise
            if call_observer:
                call_observer.finish(handle, message=result)
            return result

        try:
            return invoke(implementation, 1)
        except UpstreamError as error:
            if selected != "deepseek" or not self.settings.allow_model_fallback or not error.retryable or "ollama" not in self.settings.enabled_model_providers:
                raise
            return invoke(self._provider("ollama"), 2, True)

    def agent_model(self, provider: str | None, timeout_seconds: float):
        selected = provider or self.settings.default_model_provider
        implementation = self._provider(selected)
        return implementation.agent_client(timeout_seconds), selected, implementation.model_name

    def agent_fallback(self, provider: str, timeout_seconds: float):
        if provider != "deepseek" or not self.settings.allow_model_fallback or "ollama" not in self.providers or "ollama" not in self.settings.enabled_model_providers:
            return None
        implementation = self.providers["ollama"]
        return implementation.agent_client(timeout_seconds), "ollama", implementation.model_name

    def _provider(self, provider: str) -> ChatModelProvider:
        implementation = self.providers.get(provider)
        if implementation is None:
            raise AppError("INVALID_MODEL_PROVIDER", "不支持的模型提供商")
        if provider not in self.settings.enabled_model_providers:
            raise AppError("MODEL_DISABLED", "该模型已停用", 403)
        return implementation
