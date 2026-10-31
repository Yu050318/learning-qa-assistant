# 个人跨会话记忆固定评测

`personal-v1.json` 是合成的 120 个场景：20 开发、100 冻结。每题 1～3 个准备会话和 1 个独立提问会话，覆盖偏好、稳定事实、更正、删除、无依据问题。开发/冻结按 family 隔离；每次试验前重置记忆。不能按冻结结果逐题调参；变更语料应另建版本。

真实测量、逐题答案和完整尝试历史见 [2026-10-29～30 实测](../../docs/validation/memory-live-20261029/report.md)。原三轮因模型余额不足中断，全部失败保留；恢复后重做第二、三轮全部配对场景，与原完整第一轮组成恢复测量，不能按正确性筛选成功样本。[初始待测报告](../../docs/validation/memory-evaluation-pending/report.md)仅是接入前快照。

## 主实验

基线为现有手工记忆，实验组为同样手工记忆加自动提取。两组使用相同 `initial_memories`、准备会话和最终问题；标准事实、评审标签不作为提取输入。`gold_memories` 只用于后述受控实验。更正场景先给旧事实再给新事实；删除场景在最后提问前清空。

每组重复 3 次：冻结集共 600 个最终答案，准备会话、提取和隔离检查还会增加调用。两组交替先后执行。固定 provider、实际回答模型、上下文/工具预算、最终 Top K；实际回答模型不一致时不计算可比提升。每次最终提问使用新会话。

先配置独立测试部署，完成迁移、OpenViking 与 Memory Worker，再创建两个普通测试账号，用户名均以 `memory-eval-` 开头。令牌通过环境变量传入：

- `MEMORY_EVAL_BASELINE_TOKEN`：手工记忆组账号。
- `MEMORY_EVAL_OPENVIKING_TOKEN`：自动记忆组账号。

**脚本会删除这两个账号的手工及自动记忆。** 它校验不同身份及前缀，禁止重置普通日常账号。长运行期间应确保测试凭据有效；过期请求记为失败，不忽略。原 15 分钟 access token 可能不足，应只在隔离测试部署使用受控的较长有效期，不能修改生产认证策略或将令牌写入日志。

```powershell
# 默认离线校验，生成缺失结果报告。
.venv/Scripts/python.exe scripts/evaluate_memory.py --output tmp/memory-pending
# 真实调用需确认第三方镜像及模型凭据使用授权。
.venv/Scripts/python.exe scripts/evaluate_memory.py --run --split dev --repeats 3 --allow-reset-eval-users --output tmp/memory-dev
.venv/Scripts/python.exe scripts/evaluate_memory.py --run --split frozen --repeats 3 --allow-reset-eval-users --output tmp/memory-frozen
```

每个结果立即保存到 `predictions.json`，含答案、实际模型、证据、耗时、错误码和可用回答用量。通用脚本不自动断点续跑；中断时保留文件用于核查，余额耗尽立即停止。不要向含重复场景的预测文件追加一次新运行。本轮并发续跑驱动、尝试编号与重算命令保存在实测目录的 `reproduction/`。

## 语义评审与报告

逐题检查完整语义：全部必要事实正确、不采用被更正的旧事实、未知题应拒答。不能用关键词匹配代替正确率。`unsupported_personal_fact` 表示断言无可用依据的个人事实；删除后重复泄露也计入。corrections 类须填写 `used_updated_fact`，其他类可为 null。

脚本生成 `review-template.json`，其中空布尔值必须由评审填写，另存 `reviews.json`。`reviewer` 如实标明 human/model；模型评审的模型、提示与费用另存。`prediction_sha256` 绑定整条预测；答案、模型或证据修改后旧评审会被拒绝。

```powershell
.venv/Scripts/python.exe scripts/evaluate_memory.py --predictions tmp/memory-frozen/predictions.json --reviews tmp/memory-frozen/reviews.json --isolation tmp/memory-isolation.json --output tmp/memory-scored
```

失败答案正确性计 0。缺失预测、缺失评审和缺失安全率保持 unavailable，不通过缩小分母提高指标。报告给出执行/失败/缺失/评审覆盖及指标分母。正确率与差值的 95% 区间按场景聚类 bootstrap（seed 固定，2,000 次），不将同题的 3 次重复看作 3 个独立场景。

## 跨用户与删除检查

使用两个专用账号运行独立 canary 检查：先确认随机合成公司名已被来源用户实际提取，另一用户在新会话中提问；再删除来源用户自动记忆，在新会话提问。检查 canary、越权引用和错误可见列表。提取未完成或请求失败不计作“0 次泄漏”。

```powershell
.venv/Scripts/python.exe scripts/evaluate_memory_isolation.py --split frozen --allow-reset-eval-users --output tmp/memory-isolation.json
```

100 个冻结 case 必须全部执行，来源提取有效，跨用户及删除后召回均为 0，主实验门禁才可能通过。这只证明固定 canary 集的测试结果。

## 受控 Recall@3

把同样 `gold_memories` 和干扰事实写入 Milvus 手工记忆及独立 OpenViking 目录，比较 Top 3。它区分检索贡献与自动采集贡献，不能冒充主实验的回答正确率。

```powershell
.venv/Scripts/python.exe scripts/evaluate_memory_retrieval.py --baseline-user-id <test-user-uuid> --openviking-user-id <other-test-user-uuid> --allow-reset-eval-users --split frozen --output tmp/memory-retrieval.json
```

命令使用本机测试数据库、Milvus、Embedding 与 OpenViking，校验两个专用账号。只评有 gold 的 case，每题 3 次；保存每次 Recall@3、耗时、失败、预期分母和场景区间。OpenViking 索引未完成视作失败，不提前检索后丢掉失败记录。

## 阈值和成本

| 指标 | 建议验收目标（实际结果另报） |
| --- | --- |
| 跨会话正确率 | ≥85%，较基线提高 ≥10 个百分点 |
| 更正采用率 | ≥90% |
| 无依据个人事实误答率 | ≤5% |
| 固定集删除后召回 / 跨用户泄漏 | 各 0 次 |
| 问答 P95 | ≤基线的 110% |

最终回答耗时为实际 HTTP 问答总时间；准备会话/提取等待记录为 preparation 时间。回答、提取、Embedding、维护及评审用量分开。回答 token 仅在 ledger 完整时求和；[阶段用量](../../docs/validation/memory-live-20261029/phase-usage.json)另收集成功准备会话 ledger、OpenViking 任务返回的 LLM/Embedding Token 和处理耗时、维护任务的排队至完成耗时，并列覆盖数量。任务外查询 Embedding、失败调用账单及独立 Embedding 耗时未完整返回，保持 null，补齐供应商账单后才能计算总成本。**缺失不等于零，不预设 Token 减少。**

原 [12 文档 / 80 题 RAG 语料](../rag/synthetic_enterprise/README.md) 单独回归，保留白名单、原质量门禁及失败记录。[RAG 回归结果](../../docs/validation/memory-live-20261029/rag-regression/report.json)不作为个人记忆提升指标。
