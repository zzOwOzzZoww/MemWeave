# LongMemEval 检索层消融 v1

本协议的数据、接入方式和评分在首次运行前固定。仅测试现有 Core 检索和本地 Adapter 的输出，不训练模型，也不调用付费模型。上轮结果已用于诊断 Sibling 和 LFHV 的问题，后续成绩属于优化后复测，不是独立盲测。原始数据和逐例输出位于被 Git 忽略的 `data/`、`outputs/`，不随代码发布。

## 数据与来源

- 官方仓库：<https://github.com/xiaowu0162/LongMemEval>。
- 数据：<https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned>，`longmemeval_s_cleaned.json`，全部 500 个问题。
- 固定 revision：`98d7416c24c778c2fee6e6f3006e7a073259d48f`。
- 固定文件 SHA-256：`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。
- 本地下载使用同仓库的 `hf-mirror.com` 镜像，文件与仓库 LFS 哈希核对一致。
- 数据仓库标注 MIT 许可；不把公开合成长对话描述为真实生产用户流量。

## 冻结的接入方式

每个问题使用独立数据库，只导入其原始历史。会话按 `role: content` 展平，以固定 8,000 字符边界分块，保留全部文本，不做生成式摘要、术语增强或人工筛选。分块可能切断句子，这是接入局限，不根据答案移动边界。

标题只包含会话序号、原始时间和分块序号；搜索扩展词和显式主题词为空。原始会话 ID 可能含 `answer_`，所以这些 ID 不进入索引。`answer`、`has_answer`、`answer_session_ids` 只用于离线评分，不参与建库、归档或查询。

数据预检发现 13 个问题包含重复的原始会话 ID；按位置分别完整导入，评分时映射回原始 ID 的集合，不删除会话、不重写官方标签。

所有分块统一作为 assumed-active 的检索夹具导入。通过现有 publish/feedback 接口晋升，但夹具证据不代表真实任务验证；本实验不评估自动提取、准入、跨 Agent 迁移和最终问答效果。

## 两组实验

原生历史组：`full`、`no_bridge`、`no_sibling`、`no_anchor`。通过现有 RetrievalPolicy 关闭单项，不改 Core。

生命周期回放组：使用 `full` 作为不归档参考；另比较 `retired_lfhv` 与 `retired_no_lfhv`。按原始时间排序，把最早 `floor(会话数 / 2)` 个会话的全部分块归档，绝不参考问题或证据位置。这是固定容量回放，不是官方 LongMemEval 协议，也不是对 LFHV 自动退休策略的评价。原有历史查询规则可能直接访问归档项，LFHV-off 不意味着所有归档项都不可见。

固定 recall limit=5，slack=2，最大 7 个候选分块，上下文预算 64,000 字符。预算允许最多 7 个完整分块及审计元数据，不因实验组而改变。每个实验组和重复都从同一 active/archived SQLite 快照复制；每次使用全新的查询统计缓存。固定种子打乱组别顺序，重复两次逐例比较语义输出，计时和内部随机 ID 不参与一致性判断。

`--workers` 支持按题多进程并发，默认 1，最多 32。每题独立临时目录与数据库，同题内各组和重复串行，不使用线程共享环境变量或查询缓存。父进程按原数据顺序写出结果，语义哈希不依赖调度顺序。并发运行只加速实验，不改变评分；并发 P50/P95 含资源争用，不代表单请求延迟，也不与串行成绩直接比较。

## 指标与边界

- 正例使用官方 `answer_session_ids`。报告 Hit、平均证据会话 Recall、全部证据会话覆盖率。
- 检索指标以 Top-5 / Top-7 **分块位置**映射到会话集合；不是召回 5 / 7 个不同会话的官方 session 检索口径。重复会话分块仍消耗位置。候选命中一个会话不代表分块本身含答案。
- 另外报告实际上下文输出的证据召回、各阶段执行/增加候选/最终输出覆盖，以及配对受损和获益问题 ID。
- `_abs` 拒答题排除出正向检索分母。输出为空率单独报告，只是自定义净空诊断，不是官方问答拒答正确率。
- LFHV 另报告有证据被归档的固定子集、实际复活数量和相对不归档参考的损失；不只报告“恢复比不恢复高”。
- P50/P95 是完整本地 Adapter recall（含审计与恢复写入），不含建库、复制、Adapter 初始化、Hook 传输、答案生成。不可与 README 的 LoCoMo search-only 延迟直接比较。
- 500 个问题是评测样本数；重复调用不是独立样本。仅描述配对差异，不声称统计显著性、生产收益或领先其他框架。
- 正式运行前后校验数据、Core、脚本和本协议哈希，硬超时 60 分钟。保留全部错误与逐例输出，不按低分删题。

## 复现

```powershell
python scripts/evaluate_longmemeval_ablation.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-ablation-20260930 --repeats 2
# 多核机器上的并发复测，使用新输出目录
python scripts/evaluate_longmemeval_ablation.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-ablation-20260930-parallel --repeats 2 --workers 16
```

`--max-cases 2 --repeats 1` 仅用于接入预检，不能当正式成绩。完整运行输出 `report.json`、`REPORT.md`、`predictions.jsonl`。公开目录只保存无原始对话的协议和汇总结果。
