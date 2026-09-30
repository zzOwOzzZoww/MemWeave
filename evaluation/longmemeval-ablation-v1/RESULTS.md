# LongMemEval 检索层消融：优化后复测结果

## Material Passport

- Origin Skill: academic-research-suite（固定协议、配对比较与复现约束）
- Origin Mode: run
- Origin Date: 2026-09-30
- Verification Status: VERIFIED（语义重复输出精确一致，源码与数据冻结校验通过）
- Version Label: longmemeval-ablation-v1-rerun

## 实验概况

本轮使用 LongMemEval_S cleaned 的全部 500 个问题，比较 6 个实验臂，每臂重复 2 次，共 **6,000 次本地调用**。实验不调用模型、不访问外部服务，只测量 MemWeave 的检索与上下文注入层。

为缩短运行时间，使用 16 个隔离进程按问题并发；每个问题使用独立数据库，同一问题内部的实验臂和重复仍串行。并发只影响运行速度，不改变评分；并发下的 P95 包含机器资源争用，不作为单请求延迟宣传。

这不是首次盲测：上轮结果曾用于定位 Sibling 排位和 LFHV 满页恢复问题，因此本轮是**优化后复测**，不代表独立留出集上的泛化成绩。

## 结果

正向检索分母为 470 个问题；30 个 `_abs` 拒答题单独统计。Recall 以证据会话为单位，最多输出 7 个分块；这不是最终回答准确率。

| 实验组 | Search Hit@7 | Search Recall@7 | 实际注入 Recall | 全部证据覆盖率 | 拒答题上下文为空率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 完整系统、不归档 | 95.32% | 88.28% | 88.28% | 79.36% | 0.00% |
| 关闭 Bridge、不归档 | 95.32% | 88.28% | 88.28% | 79.36% | 0.00% |
| 关闭 Sibling、不归档 | 95.32% | 88.24% | 88.24% | 79.36% | 0.00% |
| 关闭 Anchor、不归档 | 94.26% | 86.47% | 86.47% | 77.45% | 0.00% |
| 最早一半会话归档，开启 LFHV | 95.32% | 88.74% | 88.74% | 80.21% | 0.00% |
| 同样归档，关闭 LFHV | 75.74% | 52.87% | 52.87% | 31.28% | 3.33% |

## 结果解读

- **LFHV 的生命周期价值最明显。**在固定的归档回放中，开启 LFHV 相比关闭 LFHV 的实际证据召回提高 **35.88 个百分点**（88.74% vs 52.87%），共恢复 1,368 个分块。这个结果支持“归档知识仍可按需复用并同步复活”的检索层结论，不等于最终问答收益或自动退休策略已经被证明正确。
- **Anchor 是 DBSA 中当前可观测贡献最大的阶段。**关闭 Anchor 后，不归档组 Recall@7 下降 **1.81 个百分点**（88.28% → 86.47%），18 个正例出现召回损失。
- **Sibling 的净召回影响很小。**关闭 Sibling 后 Recall@7 只下降 **0.04 个百分点**；但有 312 个问题的输出发生变化，说明它主要改变补充上下文组成，而不是稳定抬高总体召回。当前保护主结果页的仲裁规则避免 Sibling 挤掉 Direct/Bridge 结果。
- **Bridge 在本数据集上未产生可观测增益。**关闭 Bridge 与完整系统的输出和 Recall 相同；这只能说明 LongMemEval 的当前文本和固定接入方式没有覆盖它的有效触发场景，不能据此宣称 Bridge 普遍无效。

## 固定协议与边界

- 数据 revision：`98d7416c24c778c2fee6e6f3006e7a073259d48f`；数据 SHA-256：`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。
- 会话按固定 8,000 字符切块，recall limit=5、slack=2、上下文预算 64,000 字符；每组从相同 active/archived 快照复制。
- 生命周期组按原始时间归档最早 `floor(会话数 / 2)` 个会话。这是自定义容量回放，不是官方 LongMemEval 生命周期协议，也不是 LFHV 自动退休策略的完整评测。
- Search@5/@7 按分块位置统计后映射到证据会话，不是 5/7 个不同会话；命中会话不保证返回分块包含答案。
- 不评测模型回答、最终任务成功率、自动抽取准入、跨 Agent 迁移或生产规模性能；不报告统计显著性、最优归档参数或优于其他框架。
- 原始 LongMemEval 文件、逐条预测和本地运行目录不随仓库发布；复现实验需要用户自行按协议获取公开数据。

## 复现与工程校验

```powershell
python scripts/evaluate_longmemeval_ablation.py `
  --dataset data/longmemeval/longmemeval_s_cleaned.json `
  --output outputs/longmemeval-ablation-20260930-parallel `
  --repeats 2 --workers 16
```

本轮输出 6,000 条预测记录，6 组×2 次逐例语义结果精确一致，实验前后源码、协议与数据哈希一致。当前工作区全量测试为 **526 passed**，仅有 1 条既有 FastAPI/Starlette 依赖弃用警告。

机器可读汇总见 [results-20260930.json](results-20260930.json)；运行协议见 [README.md](README.md)。完整 `predictions.jsonl` 只保存在本地 `outputs/`，不进入公开仓库。
