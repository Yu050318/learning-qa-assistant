import httpx
from app.core.errors import AppError

def probe_directory(settings, provider, model_name, transport):
    state, code = "unavailable", None
    key = settings.deepseek_api_key.get_secret_value()
    if provider == "deepseek" and not key:
        raise AppError("MODEL_CREDENTIALS_MISSING", "服务端模型凭据未配置", 409)
    try:
        base = settings.deepseek_base_url if provider == "deepseek" else settings.ollama_base_url
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
