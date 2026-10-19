"""Offline by default; live generation and model review each require an explicit flag."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import TypeAdapter

from app.application.evaluation import AnswerCase, Prediction, SemanticReview, evaluate, review_digest, compare_reports, CONFIG_KEYS, digest
from app.core.errors import AppError


from app.application.evaluation_judge import JUDGE_PROMPT


def predict_live(case, services):
    from app.api.schemas import V2ChatRequest
    from app.application.chat import prepare_public_query
    from app.domain.contracts import AgentChatInput, RunContext
    from app.infrastructure.persistence.repository import Repository
    from app.workflows.supervisor import SupervisorWorkflow

    if case.user_id is None or (case.search_mode in {"knowledge", "auto"} and not case.document_ids):
        return Prediction(id=case.id, error_code="EVAL_EXPLICIT_SCOPE_REQUIRED")
    captured = []
    try:
        request = V2ChatRequest(question=case.question, search_mode=case.search_mode,
                                document_ids=case.document_ids or None, web_query=case.web_query, web_urls=case.web_urls)
        payload = AgentChatInput(case.question, case.search_mode, tuple(case.document_ids), case.web_query,
                                 web_urls=tuple(request.web_urls))
        public_query, enabled, warnings = prepare_public_query(payload, services.settings.web_search_enabled)
        if case.search_mode == "web" and not enabled and not case.web_urls:
            raise AppError("WEB_SEARCH_UNAVAILABLE", "网络搜索尚未启用", 503)
        ready = []
        if case.search_mode in {"knowledge", "auto"}:
            with services.database.sessions() as database:
                ready = Repository(database).ready_document_ids(case.user_id, case.document_ids, services.settings.embedding_model)
        # Cold isolated requests: do not read/write chat history or long-term memories, and never start ingestion workers.
        workflow = SupervisorWorkflow(services.settings, services.retriever, services.models, services.web_search,
                                      web_reader=services.web_reader, sql=services.sql)
        context = RunContext(request_id=f"eval-{case.id}", user_id=case.user_id, session_id=uuid4(),
                             question=case.question, search_mode=case.search_mode, allowed_document_ids=tuple(ready),
                             selected_provider=services.settings.default_model_provider, public_query=public_query,
                             web_enabled=enabled, web_urls=tuple(request.web_urls), warnings=warnings,
                             deadline_monotonic=monotonic() + services.settings.agent_timeout)
        result = workflow.run(context, [], "", on_evidence=captured.extend)
        if services.settings.document_versions_enabled:
            ids = [UUID(citation["document_id"]) for citation in result.citations if citation["type"] == "knowledge"]
            ids.extend(UUID(item["citation"]["document_id"]) for item in captured if item["citation"]["type"] == "knowledge")
            with services.database.sessions() as database:
                sources = Repository(database).version_sources(case.user_id, list(set(ids)))
            for citation in [*result.citations, *(item["citation"] for item in captured)]:
                if citation["type"] == "knowledge":
                    citation["document_version"] = sources[citation["document_id"]]
        return Prediction(id=case.id, answer=result.answer.content, kind=result.kind, citations=result.citations,
                          evidence=captured, run_metadata=result.run_metadata, provider=result.answer.provider,
                          model=result.answer.model, config={key: getattr(services.settings, key) for key in CONFIG_KEYS})
    except Exception as error:
        return Prediction(id=case.id, error_code=error.code if isinstance(error, AppError) else "EVAL_RUN_FAILED")


def judge_prediction(case, prediction, settings, models, provider):
    from app.workflows.agent import ModelRuntime

    timeout = min(settings.model_timeout, 30.)
    model, provider, model_name = models.agent_model(provider, timeout)
    runtime = ModelRuntime(models, model, provider, model_name, None, 1, monotonic() + timeout,
                           timeout, settings.agent_context_token_budget)
    payload = {"case": case.model_dump(mode="json"), "answer": prediction.answer, "kind": prediction.kind,
               "input_sha256": review_digest(case, prediction),
               "evidence": [item.model_dump(mode="json") for item in prediction.evidence]}
    prompt = JUDGE_PROMPT + json.dumps(SemanticReview.model_json_schema(), ensure_ascii=False)
    response = runtime.invoke([SystemMessage(content=prompt),
                               HumanMessage(content=json.dumps(payload, ensure_ascii=False))])
    if response.tool_calls:
        raise ValueError("Unexpected judge tool call")
    review = SemanticReview.model_validate_json(response.content)
    if review.reviewer != "model":
        raise ValueError("Judge must identify model review")
    review = review.model_copy(update={"judge_provider": runtime.active_provider, "judge_model": runtime.active_model_name,
                                      "judge_prompt_sha256": digest(prompt)})
    return review, {"provider": runtime.active_provider, "model": runtime.active_model_name,
                    "model_attempts": runtime.attempts, "usage_by_model": runtime.usage_by_model}


def load_items(path, schema):
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Evaluation artifact exceeds 20 MiB")
    return TypeAdapter(list[schema]).validate_json(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="答案、引用和逐事实证据评估（默认离线）")
    parser.add_argument("dataset", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--predictions", type=Path, help="离线录制的答案和本轮证据快照")
    source.add_argument("--live", action="store_true", help="显式运行真实 Supervisor，可调用收费模型/检索服务")
    reviewers = parser.add_mutually_exclusive_group()
    reviewers.add_argument("--reviews", type=Path, help="人工或已有模型逐事实审阅文件")
    reviewers.add_argument("--judge", choices=["deepseek", "ollama"], help="显式调用模型审阅，最多每题一次，不自动重试")
    parser.add_argument("--output", type=Path, required=True, help="只含评分和有限运行指标的 JSON 报告")
    parser.add_argument("--compare", type=Path, help="同一标注集的基线报告；审阅策略和有效评分题目必须一致才计算差值")
    parser.add_argument("--save-predictions", type=Path, help="可选保存完整答案及私有证据，便于人工审阅")
    parser.add_argument("--save-reviews", type=Path, help="可选保存本次模型审阅")
    args = parser.parse_args()
    inputs = {path.resolve() for path in (args.dataset, args.predictions, args.reviews, args.compare) if path}
    outputs = [path.resolve() for path in (args.output, args.save_predictions, args.save_reviews) if path]
    if len(set(outputs)) != len(outputs) or inputs.intersection(outputs):
        parser.error("输出路径不能覆盖输入或彼此重复")
    try:
        cases = load_items(args.dataset, AnswerCase)
        predictions = load_items(args.predictions, Prediction) if args.predictions else []
        reviews = load_items(args.reviews, SemanticReview) if args.reviews else []
        # Validate ID uniqueness before any network activity.
        initial = evaluate(cases, predictions, reviews)
        baseline = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
        if baseline is not None and (not isinstance(baseline, dict) or baseline.get("dataset_sha256") != initial["dataset_sha256"]):
            raise ValueError("Baseline dataset mismatch")
        if baseline is not None:
            compare_reports(initial, baseline)
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        parser.error("INVALID_EVALUATION_INPUT：检查 JSON/schema、文件大小和唯一 case.id")
    judge_events = []
    with ExitStack() as stack:
        settings, models = None, None
        if args.live or args.judge:
            from app.core.config import Settings
            settings = Settings()
        if args.live:
            from app.application.container import Services
            services = Services(settings)
            stack.callback(services.close)
            models = services.models
            predictions = [predict_live(case, services) for case in cases]
        if args.judge:
            if models is None:
                from app.infrastructure.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
                models = ModelRouter(settings, {"deepseek": DeepSeekProvider(settings), "ollama": OllamaProvider(settings)})
            by_id = {item.id: item for item in predictions}
            for case in cases:
                prediction = by_id.get(case.id)
                if prediction is None or prediction.error_code:
                    continue
                try:
                    review, trace = judge_prediction(case, prediction, settings, models, args.judge)
                    reviews.append(review)
                    judge_events.append({"id": case.id, "status": "ok", **trace})
                except Exception as error:
                    judge_events.append({"id": case.id, "status": "failed", "error_code": error.code if isinstance(error, AppError) else "EVAL_JUDGE_FAILED"})
    report = evaluate(cases, predictions, reviews)
    report.update(run_mode="live" if args.live else "offline_recorded", judge_events=judge_events,
                  semantic_method="explicit_claim_reviews" if reviews else "not_reviewed",
                  configs=[{key: prediction.config[key] for key in CONFIG_KEYS if key in prediction.config} for prediction in predictions if prediction.config])
    if args.compare:
        try:
            report["comparison"] = compare_reports(report, baseline)
        except (ValueError, OSError, KeyError, TypeError):
            parser.error("INVALID_BASELINE：基线必须是同一标注集的有效评估报告")
    for path, value in ((args.output, report), (args.save_predictions, [item.model_dump(mode="json") for item in predictions]),
                        (args.save_reviews, [item.model_dump(mode="json") for item in reviews])):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output.resolve()), "summary": report["summary"]}, ensure_ascii=False))
    return 1 if report["summary"]["failed_runs"] or report["summary"]["invalid_reviews"] or any(event["status"] == "failed" for event in judge_events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
