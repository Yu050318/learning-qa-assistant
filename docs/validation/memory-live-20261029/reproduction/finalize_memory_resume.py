"""Publish explicitly allowlisted synthetic measurements with complete attempt history."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import shutil
from statistics import mean
from xml.etree import ElementTree

from app.core.config import Settings
from app.entity.evaluation.memory import MemoryDataset
from app.service.memory_evaluation import summarize, interval, percentile
from tmp.memory_live_runtime import LOCAL

OUTPUT = Path('docs/validation/memory-live-20261029')
RESUME = 'frozen-resume-20261030'


def read(name):
    return json.loads((LOCAL / name).read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def usage(events):
    keys = ('input_tokens', 'output_tokens', 'total_tokens')
    reported = [event for event in events if event.get('usage') and all(event['usage'].get(k) is not None for k in keys)]
    return {'calls': len(events), 'reported_calls': len(reported),
            'reported_tokens': {k: sum(event['usage'][k] for event in reported) for k in keys},
            'complete': len(events) == len(reported)}


def main():
    dataset = MemoryDataset.model_validate_json(Path('evals/memory/personal-v1.json').read_text(encoding='utf-8'))
    original, original_reviews = read('frozen/predictions.json'), read('frozen/reviews.json')
    fresh, fresh_reviews = read(RESUME + '/predictions.json'), read(RESUME + '/reviews.json')
    assert len(original) == 600 and len(fresh) == 400
    assert all(row['trial'] in (2, 3) for row in fresh)
    supplement = read('original-review-supplement.json')
    supplement_events = read('original-review-supplement-events.json')
    historical = summarize(dataset, original, original_reviews + supplement)
    first = [row for row in original if row['trial'] == 1]
    first_reviews = [row for row in original_reviews if row['trial'] == 1]
    predictions, reviews = first + fresh, first_reviews + fresh_reviews
    isolation, retrieval, rag = read('isolation.json'), read('retrieval.json'), read('rag-regression/report.json')
    assert len(isolation['cases']) == 100 and len(retrieval['cases']) == retrieval['expected_trials'] == 180
    assert len(rag['cases']) == 80
    report = summarize(dataset, predictions, reviews, isolation=isolation)
    checksum = hashlib.sha256()
    for path in sorted([*Path('app').rglob('*.py'), *Path('scripts').glob('*.py')]):
        checksum.update(path.as_posix().encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    assert checksum.hexdigest() == read('resume-settings.json')['code_sha256'], 'Production source changed during resumed measurements'
    review_events = [row for row in read('frozen/judge-events.json') if row['trial'] == 1] + read(RESUME + '/judge-events.json')
    for group in ('baseline', 'openviking'):
        rows = [row for row in predictions if row['group'] == group]
        report['groups'][group]['generated_answers'] = sum(not row['error_code'] for row in rows)
        report['groups'][group]['review_usage'] = usage([e for e in review_events if e['group'] == group])
        report['groups'][group]['warning_answers'] = sum(bool(r.get('response', {}).get('run_metadata', {}).get('warnings')) for r in rows)
        by_case = defaultdict(list)
        for review in reviews:
            if review['group'] == group:
                by_case[review['case_id']].append(review)
        for metric, label in [('updated_fact_rate', 'used_updated_fact'), ('unsupported_personal_fact_rate', 'unsupported_personal_fact')]:
            values = []
            for case in dataset.cases:
                if case.split != 'frozen' or metric == 'updated_fact_rate' and case.category != 'corrections':
                    continue
                samples = by_case[case.id]
                if len(samples) != 3 or any(r.get(label) is None for r in samples):
                    values = []; break
                values.append(mean(float(r[label]) for r in samples))
            report['groups'][group][metric + '_ci95'] = interval(values)
    a, b = report['groups']['baseline'], report['groups']['openviking']
    overhead = (b['p95_ms'] / a['p95_ms'] - 1) * 100 if a['p95_ms'] and b['p95_ms'] is not None else None
    tokens_delta = ((b['answer_usage']['total_tokens'] / a['answer_usage']['total_tokens'] - 1) * 100
                   if a['answer_usage'] and b['answer_usage'] else None)
    phase_usage = read('phase-usage.json')
    rag_errors = dict(Counter(r['error_code'] for r in read('rag-regression/predictions.json') if r['error_code']))
    success_timing = {}
    for group in ('baseline', 'openviking'):
        timed = [r['elapsed_ms'] for r in predictions if r['group'] == group and not r['error_code'] and r.get('elapsed_ms') is not None]
        success_timing[group] = {'timed_successes': len(timed), 'planned': 300, 'p95_ms': percentile(timed, .95)}
    success_timing['overhead_percent'] = ((success_timing['openviking']['p95_ms'] / success_timing['baseline']['p95_ms'] - 1) * 100
                                          if all(success_timing[g]['p95_ms'] for g in ('baseline', 'openviking')) else None)
    reviewed_safety = {group: {'reviewed_answers': sum(r['group'] == group for r in reviews),
                               'unsupported_personal_fact_answers': sum(r['group'] == group and r['unsupported_personal_fact'] for r in reviews),
                               'planned_answers': 300} for group in ('baseline', 'openviking')}
    report.update(
        status='measured_with_explicit_quota_recovery', date='2026-10-30',
        attempt_policy='Complete original scheduled round 1 plus full paired new attempts for rounds 2 and 3; all original 600 retained separately.',
        original_campaign=historical, original_attempts=600, original_execution_failures=274,
        original_missing_reviews_later_supplemented=len(supplement), recovery_attempts=400,
        all_main_attempts=1000, original_campaign_quality_gate_passed=historical['quality_gate_passed'],
        recovered_measurement_quality_gate_passed=report['quality_gate_passed'],
        all_acceptance_gates_passed=bool(report['quality_gate_passed'] and rag['gate_passed']),
        openviking_image='ghcr.io/volcengine/openviking:v0.5.2.1',
        openviking_digest='sha256:7c26f9777c7632fd4b771be77471634bffb6ec8d6338b39f53deb2da1b1d34b3',
        configuration=read('frozen-settings.json'), resume_configuration=read('resume-settings.json'),
        controlled_retrieval=retrieval['summary'], controlled_index_mode='vectors_only; unreachable dummy VLM; not default semantic directory indexing',
        p95_overhead_percent=overhead, final_answer_tokens_change_percent=tokens_delta,
        successful_answers_only_latency_diagnostic=success_timing,
        reviewed_answers_only_safety_diagnostic=reviewed_safety,
        total_preparation_extraction_embedding_maintenance_currency_cost=None,
        reported_phase_accounting=phase_usage,
        rag_80_regression={'summary': rag['summary'], 'gate_passed': rag['gate_passed'], 'gate_checks': rag['gate_checks'],
                           'execution_error_counts': rag_errors},
        environment_events=read('environment-events.json'),
        limitations=[
            'Quota-interrupted original attempts are not removed; recovered quality and historical operational failures are distinct results.',
            'Round 1 ran on 2026-10-29, recovered rounds 2/3 on 2026-10-30. Host load and provider service conditions can differ.',
            'Only quota error normalization and fail-fast handling changed between source versions; no frozen-set prompt/retrieval tuning.',
            'Correctness/safety are fixed-model reviews, not full human adjudication. Synthetic questions do not establish production accuracy.',
            'Scenario bootstrap can be degenerate at all-success; related synthetic templates may remain correlated.',
            'asserted_at is source-message/manual-update provenance; aggregate memory files do not provide independent per-fact timestamps.',
            'Task-reported extraction LLM/embedding usage and successful preparation ledgers are separately counted; standalone query embedding and failed-call billing remain incomplete.',
            'Local shared-host 16-pair concurrency P95 is not a production SLA. Canary isolation checks cover this fixed set only.',
        ])
    files = (
        'dev/predictions.json', 'dev/reviews.json', 'dev/judge-events.json', 'dev/report.json', 'dev/manifest.json',
        'dev-discovery/predictions.json', 'dev-discovery/reviews.json', 'dev-discovery/manifest.json',
        'frozen/predictions.json', 'frozen/reviews.json', 'frozen/judge-events.json', 'frozen/manifest.json',
        'frozen/report-incomplete.json', 'frozen/report-incomplete.md', 'frozen/report-first-repeat.json',
        RESUME + '/predictions.json', RESUME + '/reviews.json', RESUME + '/judge-events.json', RESUME + '/manifest.json',
        'original-review-supplement.json', 'original-review-supplement-events.json',
        'isolation.json', 'retrieval.json', 'phase-usage.json', 'frozen-settings.json', 'resume-settings.json', 'environment-events.json',
        'smoke.json', 'smoke-resume.json', 'controlled-smoke.json', 'recovery-failure.json',
        'rag-regression/predictions.json', 'rag-regression/reviews.json', 'rag-regression/judge-events.json', 'rag-regression/report.json',
        'rag-regression/ingestion-manifest.json')
    for name in files:
        target = OUTPUT / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LOCAL / name, target)
    write(OUTPUT / 'recovered/predictions.json', predictions)
    write(OUTPUT / 'recovered/reviews.json', reviews)
    write(OUTPUT / 'historical-report.json', historical)
    write(OUTPUT / 'report.json', report)
    reproduction = OUTPUT / 'reproduction'; reproduction.mkdir(exist_ok=True)
    for name in ('memory_live_campaign.py', 'memory_live_runtime.py', 'memory_live_resume.py', 'memory_live_boundaries.py', 'rag_live_regression.py', 'memory_live_usage.py', 'finalize_memory_resume.py'):
        shutil.copyfile(Path('tmp') / name, reproduction / name)
    (reproduction / 'README.md').write_text('''# 复现与尝试历史

固定语料和评分：`evals/memory/README.md`、`scripts/evaluate_memory.py`。本目录保存并发驱动源代码；复制到仓库 `tmp/` 后通过 `python -m tmp.<module>` 执行。不要索引语料的标签/答案文件。

仅创建专用测试 PostgreSQL（端口 15439、`learningqa_architecture_test`）与独立 Milvus 命名空间。runtime 的 `prepare` 从本机 `.env` 读取现有配置，生成 git 忽略的秘密文件；首次执行会生成新 JWT/网关值，恢复已有状态使用 `resume-config`。配置、runtime.json、备份和原始日志不得发布。

固定 OpenViking 镜像/摘要见主报告 configuration。使用镜像默认入口、`OPENVIKING_WITH_BOT=0`，独立真实提取端口 19340。先初始化测试存储（runtime initialize），再分别启动 runtime api/worker。主驱动 `memory_live_campaign --split dev --workers 8 --repeats 3`、`--split frozen --workers 16 --repeats 3`。驱动只预置合成用户，完整对话走实际认证接口。所有输出目录拒绝覆盖。

原冻结计划执行 600 次，余额耗尽造成 274 次失败和 2 个未评审成功答案；原始文件永久保留。补充余额后，resume 驱动重做第二、三轮的全部配对场景（400 个新答案），并保留已完整完成的第一轮 200 答案，形成 recovered 的 600 答案。没有按答案正确性筛选重跑；两份 source hash、失败和环境事件均保留。历史 2 份评审后补另存，不改原文件。

`memory_live_boundaries --mode isolation --workers 8` 在 100 个冻结 case 上使用随机公司 canary 验证跨用户及删除。`--mode retrieval --workers 8` 使用独立端口 19341、同一 gold/干扰事实、TopK3、vectors_only，并将 VLM 指向不可达 dummy 地址；不是默认语义索引测试。`rag_live_regression` 只上传 manifest 指定的 12 个合成文件，运行原 80 题和原质量门禁。

评分 recovered：`python scripts/evaluate_memory.py --predictions docs/validation/memory-live-20261029/recovered/predictions.json --reviews docs/validation/memory-live-20261029/recovered/reviews.json --isolation docs/validation/memory-live-20261029/isolation.json --output tmp/memory-rescored`。历史评分另用 frozen/predictions.json 和原评审加 supplement；不将两次相同 trial 简单拼接。

真实调用使用已有明确授权，不会自动购买额度或切换模型；402 后保留结果并停止继续调用。运行完成后关闭本任务临时进程/容器，备份隔离数据库，删除配置中的密钥副本。
''', encoding='utf-8')
    lines = ['# OpenViking 真实评测与恢复记录', '', '日期：2026-10-29～30。专用测试账号、合成数据；deepseek-flash，text-embedding-v4，最终 Top K=3。', '',
             '**原冻结实验的 600 次尝试全部保留：274 次因余额耗尽执行失败，另有 2 份成功答案的评审在恢复后补齐。原实验未通过验收。**', '',
             f'原实验失败计 0 后的正确率：手工组 {historical["groups"]["baseline"]["correctness"]:.2%}（失败 {historical["groups"]["baseline"]["execution_failures"]}/300），自动组 {historical["groups"]["openviking"]["correctness"]:.2%}（失败 {historical["groups"]["openviking"]["execution_failures"]}/300）。[原实验完整评分](historical-report.json)与[原始失败](frozen/predictions.json)可复核。', '',
             '以下为恢复后的完整三轮质量测量：保留原完整第一轮，第二、三轮所有配对场景另做 400 次新尝试。共 100 个独立场景 × 3 次 × 2 组；没有按答案正确性挑选重跑。两天的服务负载及错误处理代码版本存在差异，见 JSON 指纹与环境事件。', '',
             '| 指标 | 手工记忆 | 手工＋OpenViking |', '| --- | --- | --- |']
    for label, key, fmt in [('场景尝试次数', 'executed', lambda v: str(v)), ('成功生成答案', 'generated_answers', str), ('执行失败', 'execution_failures', str),
                            ('语义评审数（模型）', 'reviewed', str), ('正确率', 'correctness', lambda v: f'{v:.2%}'),
                            ('正确率场景 bootstrap 95% 区间', 'correctness_ci95', str),
                            ('更正采用率（每组 60 次）', 'updated_fact_rate', lambda v: f'{v:.2%}'),
                            ('无依据个人事实误答率（每组 300 次）', 'unsupported_personal_fact_rate', lambda v: f'{v:.2%}'),
                            ('清空场景旧事实误答次数（每组 60 次）', 'forgetting_leaks', str),
                            ('回答 P95', 'p95_ms', lambda v: f'{v / 1000:.2f} 秒'),
                            ('最终问答已报告 Token', 'answer_usage', lambda v: f'{v["total_tokens"]:,}')]:
        lines.append('| ' + label + ' | ' + ' | '.join('不可用' if g[key] is None else fmt(g[key]) for g in (a, b)) + ' |')
    overhead_text = f'{overhead:.1f}%' if overhead is not None else '不可用（完整分母有执行失败）'
    tokens_text = f'{tokens_delta:+.1f}%' if tokens_delta is not None else '不可用（完整分母有缺失用量）'
    interval_text = [round(v, 4) for v in report['paired_delta_ci95_pp']]
    lines += ['', f'正确率提升 **{report["accuracy_delta_pp"]} 个百分点**；配对差值的场景 bootstrap 95% 区间为 {interval_text} 个百分点。',
              f'完整口径 P95 增幅：**{overhead_text}**（建议上限 10%）；完整最终问答 Token 变化：**{tokens_text}**。',
              f'仅成功回答的延迟诊断：手工组 {success_timing["baseline"]["timed_successes"]}/300，P95 {success_timing["baseline"]["p95_ms"]/1000:.2f} 秒；自动组 {success_timing["openviking"]["timed_successes"]}/300，P95 {success_timing["openviking"]["p95_ms"]/1000:.2f} 秒，增幅 **{success_timing["overhead_percent"]:.1f}%**。不能替代完整门禁或生产 SLA。',
              f'已评审的无依据个人事实断言：手工组 {reviewed_safety["baseline"]["unsupported_personal_fact_answers"]}/300；自动组 {reviewed_safety["openviking"]["unsupported_personal_fact_answers"]}/{reviewed_safety["openviking"]["reviewed_answers"]}，另有 1 次执行失败未评审，正式安全分母仍为 300。',
              '本轮提取失败的原因为冻结配置 `vlm.max_tokens=2048` 下输出被截断；未在试验中途调高预算或覆盖失败，见 [失败诊断](recovery-failure.json)。', '',
              '## 独立检查', '', f'- 跨用户/单条删除 canary：{isolation["summary"]["source_verified"]}/100 来源提取已验证，执行失败 {isolation["summary"]["execution_failures"]}，跨用户泄漏 {isolation["summary"]["cross_user_leaks"]}/100，删除后召回 {isolation["summary"]["deleted_recalls"]}/100。100 个随机公司名、8 对账号复用，每次清空并更换 generation；不是 100 对独立用户。',
              f'- 同事实受控 Recall@3：Milvus {retrieval["summary"]["baseline_recall_at_3"]:.2%}，OpenViking {retrieval["summary"]["openviking_recall_at_3"]:.2%}，共 180 次、失败 {retrieval["summary"]["execution_failures"]} 次。使用 vectors_only 和不可达 dummy VLM；该结果不证明检索质量提升。',
              f'- 原 80 题 RAG：成功 {rag["summary"]["successful_runs"]}/80，失败 {rag["summary"]["failed_runs"]}（{rag_errors}），门禁 {"通过" if rag["gate_passed"] else "未通过"}。逐题与原阈值见 [RAG 报告](rag-regression/report.json)。它是独立回归，不是个人记忆基准。', '',
              f'恢复质量门禁：**{"通过" if report["quality_gate_passed"] else "未通过"}**。本轮综合验收：**{"通过" if report["all_acceptance_gates_passed"] else "未全部通过"}**。历史原实验仍未通过，所有执行失败完整保留，不能用续跑结果抹去。', '',
              '## 用量、限制与复现', '',
              '最终问答和评审用量在 JSON 中分列；[阶段用量](phase-usage.json)另计原实验/续跑中成功准备会话的 ledger、任务返回的提取 LLM/Embedding Token 与处理耗时、维护排队至完成耗时。任务外查询 Embedding、失败调用及完整维护账单仍缺失，不补零，不宣称总 Token 或费用下降。原记录、续跑、后补评审和评分输入分别存放；[复现说明](reproduction/README.md)给出重算入口。', '',
              '所有语义标签由同一固定模型评审，尚无完整人工复核。合成问题较简单，全对结果及退化的 bootstrap 区间不外推为生产保证；相关模板也可能存在场景相关性。删除与隔离结论只覆盖本固定 canary 测试。',
              '时间元数据来自来源消息或手工更新；聚合文件中的每个事实没有独立的时间精度。默认只提取 profile/preferences/entities，不启用任务经验学习。', '']
    lines += ['### 已报告的阶段用量（不是完整账单）', '',
              '| 尝试范围 | 成功准备会话 Token | 提取 LLM Token / 有用量回执 | 任务内 Embedding Token | 任务处理 P95 |',
              '| --- | --- | --- | --- | --- |']
    for name, row in phase_usage['groups'].items():
        preparation = row['preparation_answers']['usage']
        extraction = row['automatic_extraction']
        llm = extraction['reported_llm_usage']; embedding = extraction['reported_task_embedding_usage']
        applicable = extraction['jobs_with_task_receipt'] > 0
        lines.append('| ' + name + f' | {preparation["reported_sum"]["total_tokens"]:,}（{preparation["reported_records"]}/{preparation["expected_records"]} 个成功回答） | ' +
                     (f'{llm["reported_sum"]["total_tokens"]:,}（{llm["reported_records"]}/{llm["expected_records"]} 回执）' if applicable else '不适用') + ' | ' +
                     (f'{embedding["reported_sum"]["total_tokens"]:,}' if applicable else '不适用') + ' | ' +
                     (f'{extraction["reported_processing_seconds"]["p95"]:.2f} 秒' if applicable else '不适用') + ' |')
    lines += ['', '范围分别为原全部尝试与续跑全部尝试，包含中断失败的准备过程，不等于恢复质量测量的 600 答案成本。回执不存在、回执未返回用量、独立查询 Embedding 与维护账单不补零。提取任务耗时不能拆作纯 LLM/Embedding 各自耗时，维护耗时包含排队。', '']
    (OUTPUT / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    private = read('runtime.json'); settings = Settings()
    secret_values = [settings.deepseek_api_key.get_secret_value(), settings.dashscope_api_key.get_secret_value(), *private.values()]
    for path in OUTPUT.rglob('*'):
        if path.is_file() and any(secret and secret in path.read_text(encoding='utf-8') for secret in secret_values):
            raise RuntimeError('Credential found in public artifact: ' + path.name)
    print(json.dumps({'report': str(OUTPUT / 'report.md'), 'accuracy_delta_pp': report['accuracy_delta_pp'],
                      'p95_overhead_percent': overhead, 'recovered_gate': report['quality_gate_passed'],
                      'isolation': isolation['summary'], 'rag_gate': rag['gate_passed']}))


if __name__ == '__main__':
    main()
