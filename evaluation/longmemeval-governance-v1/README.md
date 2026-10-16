# LongMemEval 自动准入审计 v1

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: run
- Origin Date: 2026-10-16
- Verification Status: PROTOCOL_FROZEN_BEFORE_RUN
- Version Label: longmemeval-governance-audit-v1

## 要回答什么

在固定原文候选输入下，现有自动治理会保留多少、拦截多少、自动晋升多少？官方标注为后续回答提供证据的消息，有没有被这道准入门槛挡掉？

这是公开数据上的**自动准入诊断**，不是官方治理 benchmark 成绩，也不是完整 AI 治理系统成绩。分级准入是本轮被测对象；MCP 提案规划、精确归并、冲突替代、LFHV 和最终问答不在本协议里。现有单元测试验证这些操作的边界，不能替代它们的效果评估。

## 数据与隔离

- 官方仓库：<https://github.com/xiaowu0162/LongMemEval>。
- 数据：<https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned> 的 `longmemeval_s_cleaned.json`，全部 500 题。
- 固定 revision：`98d7416c24c778c2fee6e6f3006e7a073259d48f`。
- SHA-256：`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。
- 每题独立临时 SQLite，原文会话按原始时间排序。只使用公开对话，不读取生产库或本机模型凭据，不调用模型、不花 API 费用。
- 500 题已用于既有检索诊断，不称为新盲测。完整数据、逐条结果和原文 badcase 均留在被 Git 忽略的 `data/`、`outputs/`；公开目录只保存协议、脚本说明和无原文的汇总。

## 冻结的候选构造

1. 每条用户消息作为一份完整原文候选，原样保留 `source_quotes`；不看问题、答案或证据标注来挑选句子。
2. 确定性适配器仅按原文中的 `prefer / preference / always / from now on / by default / remember` 词面设置 preference，否则设置 fact；标题取归一空白后的前 120 字符。这不是独立分类金标准，不证明真实模型会如此提取。
3. 全部候选统一传入 `retention=durable`，让当前 Core 自己拦截能识别的临时性、猜测、敏感内容、问题与无授权内容。这是刻意宽松的输入压力审计，**不包含真实提取模型的 retention 分类收益**。
4. 整轮超过 8,000 字符的文本不截断、不偷偷挑句子，计为 `adapter_oversize`，留在分母；真实模型通常会提取较短断言，所以这里是整轮适配器的局限，不归因为 Core 过滤失败。
5. 将原始用户和紧随其后的助手回复构造为无工具证据的 TranscriptTurn，通过真实 `LearningEngine.learn()` 执行编译、入库、去重、自动晋升和审计。不会把公共数据标签伪造成测试成功或用户批准。
6. 会话与轮次用位置别名，原始 ID 中的 `answer_` 等信息不进入 Core。所有题目独立且使用相同项目名，避免跨题用户级知识泄漏。
7. 以完整历史执行后的实际数据库状态为准，不把“提取了一个 proposal”当作“成功入库或生效”。

## 指标

- 全部用户消息的 `filtered / candidate / active / adapter_oversize` 分布、具体拦截原因、实际自动晋升次数、唯一记录状态。
- 官方 `has_answer=true` 用户消息的**保留率（包含待审）**与 **active 比率**分别汇报。候选保留不代表已经能被普通召回。
- 正例题中有用户证据的题目单独计算“至少保留一条 / 全部保留”的题数；只有助手证据的题目不伪算成用户证据零命中。助手消息没有实际工具观察，本协议不测试其独立准入；另报告数量。
- `_abs` 题不进入正向证据分母，但其全部用户消息照常参与治理负载与处理分布。
- `has_answer` 是整条消息的证据标记，不是具体断言的审批真值；消息被过滤只是需要检查的证据损失，不自动等同于产品政策错误。生活事件可对后续问答有用，但并不一定适合 Coding Agent 默认长期存储。
- 未标注 `has_answer` 不代表垃圾，因此不报告“正确过滤率”；没有独立治理标签，`auto_accept_precision` 记为 null，不报告虚构的“自动放行准确率”。
- 不把所有消息、重复语料或重复运行当成独立样本，不宣称统计显著性或真实人工工作量节省。
- learn 的 P50/P95 包含 SQLite/编译/审计，不包含提取模型；并发计时包含资源争用，不能与 README 的 search-only 延迟比较。

## 运行与验收

```powershell
python scripts/evaluate_longmemeval_governance.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-governance-20261016 --workers 16
```

`--max-cases 2` 只用于接入预检，必须使用新输出目录。运行前后核验数据和源码哈希，不修改治理逻辑、不根据结果调参或挑题。逐题打印进度并写入 `cases.jsonl`，不自动重试、跳过异常或覆盖旧输出；异常退出时保留部分记录但不生成完整汇总。硬超时默认 30 分钟；超时应先告知用户，再停止本轮实验。

验收需要 500 题全部完成、分母对齐、数据/源码哈希未变和评测脚本测试通过。若效果不好，照实记录，不通过自动批准或缩小分母美化成绩。
