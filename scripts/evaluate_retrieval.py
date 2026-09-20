"""Evaluate the live knowledge retriever against manually labelled queries."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from app.application.retriever import RetrieverService
from app.core.config import Settings
from app.infrastructure.embeddings.qwen import QwenEmbedding
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


def score_rankings(rankings: list[tuple[list[str], set[str]]], ks: list[int]) -> dict:
    if not rankings:
        raise ValueError("评估集不能为空")
    summary = {}
    for k in ks:
        recalls = []
        hits = []
        for retrieved, relevant in rankings:
            if not relevant:
                raise ValueError("每道题至少需要一个 relevant_chunk_id")
            matched = relevant.intersection(retrieved[:k])
            recalls.append(len(matched) / len(relevant))
            hits.append(bool(matched))
        summary[f"recall@{k}"] = round(sum(recalls) / len(recalls), 4)
        summary[f"hit_rate@{k}"] = round(sum(hits) / len(hits), 4)
    reciprocal_ranks = [
        next((1 / rank for rank, chunk_id in enumerate(retrieved, 1) if chunk_id in relevant), 0)
        for retrieved, relevant in rankings
    ]
    summary["mrr"] = round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="计算当前 Milvus + Embedding 检索链路的召回指标")
    parser.add_argument("dataset", type=Path, help="JSON 评估集路径")
    parser.add_argument("--ks", default="1,3,6", help="逗号分隔的 K，默认 1,3,6")
    args = parser.parse_args()

    ks = sorted({int(value) for value in args.ks.split(",") if int(value) > 0})
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    settings = Settings()
    retriever = RetrieverService(QwenEmbedding(settings), MilvusVectorStore(settings), max(ks))
    rankings = []
    details = []

    for case in cases:
        relevant = set(case["relevant_chunk_ids"])
        hits = retriever.retrieve(
            UUID(case["user_id"]),
            case["question"],
            [UUID(value) for value in case["document_ids"]],
        )
        retrieved = [hit.chunk.chunk_id for hit in hits]
        rankings.append((retrieved, relevant))
        details.append({
            "id": case["id"],
            "retrieved_chunk_ids": retrieved,
            "relevant_chunk_ids": sorted(relevant),
        })

    print(json.dumps({"cases": details, "summary": score_rankings(rankings, ks)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
