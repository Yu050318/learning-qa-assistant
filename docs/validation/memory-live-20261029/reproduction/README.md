# 复现与尝试历史

固定语料和评分：`evals/memory/README.md`、`scripts/evaluate_memory.py`。本目录保存并发驱动源代码；复制到仓库 `tmp/` 后通过 `python -m tmp.<module>` 执行。不要索引语料的标签/答案文件。

仅创建专用测试 PostgreSQL（端口 15439、`learningqa_architecture_test`）与独立 Milvus 命名空间。runtime 的 `prepare` 从本机 `.env` 读取现有配置，生成 git 忽略的秘密文件；首次执行会生成新 JWT/网关值，恢复已有状态使用 `resume-config`。配置、runtime.json、备份和原始日志不得发布。

固定 OpenViking 镜像/摘要见主报告 configuration。使用镜像默认入口、`OPENVIKING_WITH_BOT=0`，独立真实提取端口 19340。先初始化测试存储（runtime initialize），再分别启动 runtime api/worker。主驱动 `memory_live_campaign --split dev --workers 8 --repeats 3`、`--split frozen --workers 16 --repeats 3`。驱动只预置合成用户，完整对话走实际认证接口。所有输出目录拒绝覆盖。

原冻结计划执行 600 次，余额耗尽造成 274 次失败和 2 个未评审成功答案；原始文件永久保留。补充余额后，resume 驱动重做第二、三轮的全部配对场景（400 个新答案），并保留已完整完成的第一轮 200 答案，形成 recovered 的 600 答案。没有按答案正确性筛选重跑；两份 source hash、失败和环境事件均保留。历史 2 份评审后补另存，不改原文件。

`memory_live_boundaries --mode isolation --workers 8` 在 100 个冻结 case 上使用随机公司 canary 验证跨用户及删除。`--mode retrieval --workers 8` 使用独立端口 19341、同一 gold/干扰事实、TopK3、vectors_only，并将 VLM 指向不可达 dummy 地址；不是默认语义索引测试。`rag_live_regression` 只上传 manifest 指定的 12 个合成文件，运行原 80 题和原质量门禁。

评分 recovered：`python scripts/evaluate_memory.py --predictions docs/validation/memory-live-20261029/recovered/predictions.json --reviews docs/validation/memory-live-20261029/recovered/reviews.json --isolation docs/validation/memory-live-20261029/isolation.json --output tmp/memory-rescored`。历史评分另用 frozen/predictions.json 和原评审加 supplement；不将两次相同 trial 简单拼接。

真实调用使用已有明确授权，不会自动购买额度或切换模型；402 后保留结果并停止继续调用。运行完成后关闭本任务临时进程/容器，备份隔离数据库，删除配置中的密钥副本。
