# LongMemEval 端到端对照 v1

目标：检验正确历史证据被召回之后，是否改善模型的最终回答。这里的端到端仅指“原始历史导入 → 检索 → 上下文 → 回答 → 评分”，不包括自动学习准入、真实 Agent Hook 传输或编码任务执行。

## 数据与固定条件

复用公开 LongMemEval_S cleaned 全部 500 道题。revision 和 SHA-256 与 [检索消融协议](../longmemeval-ablation-v1/README.md)一致。该数据此前用于诊断和优化，**不能称作独立盲测或新留出集**。默认先以固定种子按题型和拒答标签分层均衡抽取 12 题检查流程；这是诊断试跑，不代表全量分布，不作为简历成绩。

只按原始会话的 role/content 和日期建库；每 8,000 字符分块。answer、has_answer 和证据 ID 不进入索引或回答提示词。原始历史统一作为 assumed-active 夹具导入，因此不能证明学习提炼和证据准入的效果。各组复制独立 SQLite 快照，使用相同原始历史、查询、limit=5、slack=2 和 64,000 **字符**预算；字符数不是 Token 数。普通 FTS5 基线按同样的词法 tokenizer 和索引 BM25 排序，不设最低相关性门禁、不运行 DBSA，也不访问归档知识。

## 两个场景、七组配置

| 场景 | 组别 | 目的 |
|---|---|---|
| native：不归档历史 | no_memory / fts5 / memweave | 比较无历史、普通词法检索、完整框架 |
| retired：按时间归档最早一半历史 | no_memory / fts5 / memweave / no_lfhv | 比较同样归档压力下的收益，并隔离 LFHV |

归档不参考答案位置。这是自定义容量回放，**不是官方 LongMemEval 场景，也不是自动淘汰策略验证**。原有历史查询可能访问归档知识，关闭 LFHV 不等于所有归档信息绝对不可见。归档组单独报告，不与 native 混成一个提升数字；另报告答案证据确实被归档的正例子集。

MemWeave 先运行真实 Core + Adapter 的召回、预算与恢复逻辑，再将实际输出的完整原始分块用共同格式交给回答模型。不包含内部知识 ID、来源徽章、状态标签或实验组名，避免提示词包装成为混杂因素。这种规范化评测也不是原生 Hook 注入文本的验收。若共同包装导致原本实际输出的分块被丢弃，准备流程直接失败，不偷偷改变 LFHV 复活与回答输入的对应关系。

## 回答与评分

同一回答模型、temperature=0；回答与 Judge 的输出预算默认均为 2048 Token，允许提高，不允许低于 2048。所有组都要求只根据历史回答，证据不足应拒答。提供 question_date；模型从不见标准答案。生成模式沿用服务商默认：DeepSeek 接口文档当前注明默认为思考模式，temperature 在该模式下不生效，输出预算也必须给思考留余量。2048 是生成上限，不代表必须输出 2048 个 Token。模型输出未完成、为空、请求失败均明确记为错误，保留该题，不自动重试；记录白名单 finish_reason、usage 和错误原因，不记录失败响应正文或思考文本。API usage 缺失时 Token 指标记为不可用，不按字符估算成 Token。

2026-10-13 配置修订 `2048-budget-r1`：原试跑实际发送 150 次请求，但回答预算 512、Judge 预算仅 10，未形成有效评分。旧失败产物保留，新一轮重新冻结相同种子、题目和检索参数；不把失败轮算作 0% 正确率，也不混合两轮结果。服务参数依据 [DeepSeek 官方 API 文档](https://api-docs.deepseek.com/api/create-chat-completion)。

评分提示词来自 [官方 evaluate_qa.py](https://github.com/xiaowu0162/LongMemEval/blob/d6dc8b50a2d9ac0c99485ea28fa5755c62414c34/src/evaluation/evaluate_qa.py)，原文按题型保留，包括时间题容忍一天误差、知识更新和 preference rubric。上游文件 SHA-256：`ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251`。上游代码 MIT 许可，Copyright (c) 2024 Di Wu；许可见 [UPSTREAM-LICENSE](UPSTREAM-LICENSE)。

Judge 仅看到题目、标准答案和生成答案，不看到检索上下文、组名或场景。Judge 模型单独指定；采用官方提示词不等于采用官方指定评分模型，不作为官方榜单成绩。yes/no 严格解析，含混输出视为评分错误。若回答与 Judge 使用同一模型，必须披露同模型评分偏差；正式结果还需要人工抽查配对变化、拒答和评分争议案例。temperature=0 不保证模型完全确定，单次试跑不证明统计显著性。

## 指标与公开范围

报告各场景回答正确率、正例正确率、拒答正确率、题型细分、实际 API 输入 Token、检索 P95、检索加回答 P95，并保留配对获益/受损数。未完成任一题时，该组正式正确率与配对提升记为不可用；另列正确数/全部计划题数的下界，避免按成功请求缩小分母。耗时不包括建库/初始化/快照复制或 Judge，不是 TTFT；并发耗时含负载影响。

无记忆组主要是 sanity check；MemWeave 对普通 FTS5 的配对差异才是框架收益比较。LFHV 比较限于 retired 场景。试跑不报告显著性，完整实验需在看结果前固定重复次数和统计方案，不能用试跑最优配置挑成绩。

离线 prepare 不调用外部模型。live run 须显式同意发送公开题目、检索片段和评分标准答案，同时指定两个 API 的地址/模型、凭据环境变量名和调用上限。脚本不读取用户真实知识库、私有对话或启动器密钥，不记录凭据。模型、usage、响应和错误保留在被 Git 忽略的 outputs/。公开汇总须人工检查后再加入本目录；不自动上传 GitHub，不自动改简历。

运行前后校验源代码、协议、准备输入哈希。输出目录必须不存在，防止覆盖旧结果；无静默续跑或自动重试。

## 使用

离线试跑准备（零模型调用）：

```powershell
python scripts/evaluate_longmemeval_e2e.py prepare --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-e2e-pilot-prepared --sample-size 12 --workers 4
```

明确授权后使用以下命令调用回答与评分服务。12 题 × 7 组 × 2 请求，最多 168 次 API 调用；不是 168 个独立样本。

```powershell
python scripts/evaluate_longmemeval_e2e.py run --prepared outputs/longmemeval-e2e-pilot-prepared --output outputs/longmemeval-e2e-pilot-live --consent-public-data --max-api-calls 168 --answer-base-url https://api.deepseek.com/v1 --answer-model deepseek-flash --judge-base-url https://api.deepseek.com/v1 --judge-model deepseek-flash --answer-key-env DEEPSEEK_API_KEY --judge-key-env DEEPSEEK_API_KEY --answer-max-tokens 2048 --judge-max-tokens 2048 --workers 4
```

`--sample-size 500` 为完整数据，计划 3,500 次回答和最多 3,500 次评分调用。先检查试跑流程与费用，再确认全量调用，不能把本地召回次数当成模型 API 次数。

当前状态：协议和执行器已建立，**真实端到端成绩尚未产生**。
