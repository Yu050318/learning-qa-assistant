import math

import httpx

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError


class QwenEmbedding:
    def __init__(self, settings: Settings):
        self.settings = settings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        key = self.settings.dashscope_api_key.get_secret_value()
        if not key:
            raise AppError("CONFIGURATION_ERROR", "请配置 DASHSCOPE_API_KEY", 503)
        vectors = []
        try:
            with httpx.Client(timeout=self.settings.model_timeout) as client:
                for offset in range(0, len(texts), self.settings.embedding_batch_size):
                    batch = texts[offset:offset + self.settings.embedding_batch_size]
                    response = client.post(
                        f"{self.settings.dashscope_base_url.rstrip('/')}/embeddings",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": self.settings.embedding_model, "input": batch,
                              "dimensions": self.settings.embedding_dimension, "encoding_format": "float"},
                    )
                    response.raise_for_status()
                    entries = sorted(response.json()["data"], key=lambda entry: entry["index"])
                    if [entry["index"] for entry in entries] != list(range(len(batch))):
                        raise ValueError("Embedding response count or order mismatch")
                    for entry in entries:
                        vector = entry["embedding"]
                        if len(vector) != self.settings.embedding_dimension or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector) or not any(vector):
                            raise AppError("EMBEDDING_DIMENSION_MISMATCH", "向量维度或数值与配置不一致", 502)
                        vectors.append(vector)
            return vectors
        except AppError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise UpstreamError("embedding") from None

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
