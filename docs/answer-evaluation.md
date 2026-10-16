# 第六阶段：可复现的答案忠实度与引用支持度评估

## 本阶段交付与复用

新增 `app/application/evaluation.py` 评分模块和 `scripts/evaluate_answers.py` CLI，复用现有 Pydantic 引用 schema、Supervisor、模型适配器、ModelRuntime 和权限检索。没有新增依赖、数据库表、网页或评估框架。

原有 `evaluate_retrieval` 继续负责 Recall@K / Hit Rate@K / MRR；新工具评估完整答案，二者不互相替代。此前 `rag_v2_cases.json` 是题目提纲，没有标准事实和语义审阅，不能直接当作完整答案质量基准。本阶段另提供十题稳定的合成样例，覆盖 Excel、SQL＋Web、无答案、澄清、闲聊和错误注入。

Supervisor 仅增加可选进程内 `on_evidence` 回调：最终引用校验成功后，输出本轮可用的原始叶子证据与对应来源卡元数据。默认不调用；它不会进入 SSE、ChatService 消息元数据或新增存储字段。没有编号的父块与 Grader 排除的候选不会进入该快照。

## 指标如何解释

| 指标 | 计算对象与含义 |
| --- | --- |
| kind_correct | 回答种类是否符合该题标注；执行失败记 0 |
| citations_valid | 正文引用与来源卡、获准快照、模式和显式文档范围是否一致；grounded 必须有引用；多余/重复/伪造卡片失败 |
| citation_validity | 正文中合法的唯一引用编号 / 正文所有唯一引用编号，无引用时 null |
| source_types_correct | 实际合法引用的类型集合是否符合预期类型集合 |
| faithfulness | 被审阅的事实主张中，语义标签为 supported 的比例 |
| citation_support | 带编号主张中，内容受支持且所附编号均对应支持引句的比例 |
| claim_citation_coverage | 审阅主张中带编号的比例；有编号不代表引用正确 |
| fact_coverage | 标准事实中被审阅为 covered 的比例；missing / incorrect 不计覆盖 |

每个指标同时保存 `scored_cases`；汇总是有效题目的宏平均，并按 category 分组。失败和缺失的运行单列计数，不将它们计成有效语义结果。没有审阅、不适用的指标、模型判断失败和非法审阅均为 null，不能解读为 0 或 1。报告不产生一个把结构和语义混成一项的“总通过率”。

合法引用只证明来源编号与本轮快照一致，不能证明语义支持。例如“保修十年[1]”确实引用功率片段，引用合法但事实不受支持；正确的功率事实若把 Excel 行号改错，语义事实可能受支持，但来源合法性与引用支持度失败。

语义评估来自人工或显式模型审阅，评分器本身不靠关键词猜测蕴涵。审阅者必须提取所有可核查事实，包括无引用的事实，逐项给出 supported / unsupported / contradicted / unverifiable。supported / contradicted 需提供真实叶子中的逐字引句。评分器校验引句、编号和答案片段，拒绝伪造引句、未知来源、重叠/重复主张和陈旧审阅。

这些校验无法机械证明主张提取完整或引句确实蕴涵结论。模型裁判可能受注入或同源偏差影响，即使输出结构合规也可能判断错误；报告明确保存审阅来源，应抽样人工复核。faithfulness 是“已审阅主张的忠实度”，不是未经审计的事实正确率。

## 离线运行与样例结果

```powershell
.venv/Scripts/python.exe -m scripts.evaluate_answers tests/fixtures/answer-evaluation/cases.json --predictions tests/fixtures/answer-evaluation/predictions.json --reviews tests/fixtures/answer-evaluation/reviews.json --output data/evaluations/offline.json
```

此命令不创建 Services、不加载真实 `.env`、不联网、不调用任何收费模型。没有 `--reviews` 时仍评估引用结构，所有语义指标保持空值。

仓库中的[样例报告](evaluation/answer-smoke-report.json)由同一命令生成，只将输出位置改为 `docs/evaluation/answer-smoke-report.json`。十题中故意包含错行号、伪造编号、保修幻觉和把父块信息错误归到叶子等反例；它只验证评分器能检出这些错误，**不能代表真实系统准确率或提升幅度**。

报告包含标注集、答案快照、审阅内容的 SHA-256 指纹（校验后规范化 JSON，不随文件缩进变化）、逐题指标、分组指标、执行错误和有限调用统计。默认报告没有问题/答案/私有证据正文；`--save-predictions` 才保存完整答案和证据，`--save-reviews` 保存审阅。含私有材料的本地文件应放在被忽略的 `data/evaluations/`，不要提交到仓库。输出路径不能覆盖输入或互相重复。

## 标注文件契约

三个文件均为 JSON 数组，case.id 唯一且一致；详见 `tests/fixtures/answer-evaluation/` 的可运行示例。

- cases：`id/category/question/search_mode/expected_kinds/expected_source_types/expected_facts`；真实运行另填 `user_id/document_ids/web_query/web_urls`。
- predictions：`id/answer/kind/citations/evidence`，其中 evidence 为 `{citation: 原来源卡元数据, content: 获准叶子全文}`；失败运行填 `error_code`。可选保存模型、有限配置和 run_metadata。
- reviews：`id/reviewer/input_sha256/claims/fact_coverage`。每个 claim 的 `text` 是答案中唯一且不重叠的原文片段，包含紧随的 `[编号]`；quotes 为 `{number,text}`；fact_coverage 与 expected_facts 顺序一一对应。模型审阅还必须保存 `judge_provider/judge_model/judge_prompt_sha256`；CLI 按实际运行模型和提示记录，不信任模型自行申报的身份。

审阅摘要绑定当前题目、答案、引用和快照，防止改了答案或标准事实后继续使用旧标签。人工标注前用下面的函数获取应填指纹：

```python
from app.application.evaluation import review_digest
fingerprint = review_digest(case, prediction)  # case / prediction 是已校验的模型对象
```

离线评分无法证明输入快照的来源真实性，应使用受信任的实际采集文件。人为篡改快照再给自己打分不属于有效验证。

## 显式在线运行

```powershell
# 读取真实业务配置，调用现有 Supervisor；可能产生模型、检索和网页服务费用
.venv/Scripts/python.exe -m scripts.evaluate_answers data/evaluations/labelled.json --live --output data/evaluations/live.json --save-predictions data/evaluations/live-predictions.json

# 复用已录制答案，用现有 DeepSeek 适配器进行模型审阅；也可选 ollama
.venv/Scripts/python.exe -m scripts.evaluate_answers data/evaluations/labelled.json --predictions data/evaluations/live-predictions.json --judge deepseek --output data/evaluations/judged.json --save-reviews data/evaluations/model-reviews.json
```

`--live` 和 `--judge` 分别显式开启生成与语义审阅，默认都不启用。真实请求必须指定 user_id；knowledge/auto 还必须显式指定文档 ID，先通过现有 Repository 校验所有权、ready 状态和模型一致性，不能隐式遍历全部资料。SQL/Web 仍使用现有白名单和 URL 授权，不改变连接配置。该命令是受信任的本地运维入口，不是新增的免认证 HTTP 接口。

每题执行隔离的冷启动问答，没有会话历史或长期记忆，既不写入 ChatService 消息，也不启动入库或恢复任务。因此它评估当前 Supervisor 及其子 Agent，不宣称覆盖历史压缩/长记忆效果。实际工作流仍使用原请求 deadline、模型和工具预算；模型审阅单独最多一次调用、最多 30 秒，不自动降级或重试，仍由 ModelRuntime 执行上下文限制，预算不够时保留失败记录，不截断证据来制造成功分数。

模型只接收要审阅的答案、标准事实和获准叶子；私有文本会发送给所选模型。报告中的模型审阅与人工审阅分开标识；不能把在线 Grader 判断或答案生成模型的自评当作独立的人类金标。

## 同标注集基线对比

固定文档/用户范围、模型和标注集，分别启停 `RERANK_ENABLED`、`KNOWLEDGE_QUALITY_ENABLED` 后采集答案，再分别审阅。每份报告会保存非敏感的配置白名单，不保存连接串或密钥。

```powershell
.venv/Scripts/python.exe -m scripts.evaluate_answers data/evaluations/labelled.json --predictions data/evaluations/variant-predictions.json --reviews data/evaluations/variant-reviews.json --compare data/evaluations/baseline.json --output data/evaluations/comparison.json
```

必须使用相同标注集指纹。同一指标只有在有效评分题目集合相同、语义审阅策略一致时才计算差值；审阅缺失或裁判模型变化时，相关差值为空，不把覆盖变化算作质量提升。报告保留基线答案指纹。输入或基线错误返回退出码 2；运行缺失、执行失败、非法审阅或裁判调用失败返回 1；成功输出报告返回 0。退出码 0 不表示所有质量指标为 1，质量门槛需由使用者根据基线另行制定。

## 验证与边界

```powershell
.venv/Scripts/python.exe -m pytest tests/test_answer_evaluation.py -q
.venv/Scripts/python.exe -m pytest -q
```

本阶段只验证评估实现与合成控制样例，没有调用真实 Milvus、Tavily、业务 SQL 或收费模型；未测量生产答案准确率。文档不可变版本、可靠 Worker、可恢复 Run/Event、共享权限与模型中心仍属于后续独立阶段。

2026-10-16 验证记录：后端全量 **235 passed、9 skipped、38 subtests passed**；答案评估专项 **27 passed**。离线报告生成、同输入基线对比（差值为 0）、CLI help、`uv lock --check` 和 diff 空白检查通过。跳过项需要外部隔离服务；未新增依赖、未修改前端、未运行真实在线生成/模型裁判、未提交或推送。
