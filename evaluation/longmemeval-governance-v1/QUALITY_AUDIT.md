# v9：先看保留内容，再看候选数量

## 问题与范围

v8 的 455 条新增标注消息只证明“有片段保留”，不能当作答题证据恢复。原文复核发现关系从句 `which …` 被误当成问题、计划/请求与无主体回指被保留等问题；同正文换标题还会生成重复待审记录。

本轮只修 Core 的独立断言边界和学习入口的精确候选复用，补离线内容审计与隔离 Runtime 闭环验收。不扩大 Agent 接入、不改检索/LFHV/Hook、不根据标准答案挑句，不自动批准普通事实，不部署生产 Runtime。

## 变化

- 逗号只有在明确问句结构时才切开；`which has / which is / which I …`、关系从句 `where …` 保持在事实中。无法区分的表达保守待审或拒绝，不靠裸疑问词切句。
- 识别过去时、缩写和话语前缀后的计划/寻求帮助，以及脱离主体的 `it / this / that` 回指。仅用于被拒混合提案的受限 fallback，不重写其他合法提案。
- 学习入口可按完全一致的正文、类型、范围、项目、说话人复用未过期待审候选，保留每次编译的来源。标题不再制造新候选；不模糊合并、不跨项目合并、不复用已生效/隔离/归档状态，不覆盖第一条索引元数据。手动发布及显式治理合并权限不变。

## 内容审计方法与限制

`scripts/audit_governance_retention.py` 仅离线读取已完成回放的原始输入及实际保留偏移。先核对 500 题、全部用户消息、原文 SHA、偏移顺序及 full/partial/none，缺失即失败；不缩分母、不拼接不连续片段制造答案。

对全部官方标注消息分别报告三种**字面诊断**：标准答案完整词序在保留片段中、标准答案在原文但已不在保留片段中、标准答案本就不以该字面形式出现在原文。采用固定 Unicode/大小写归一化及完整词序匹配，不选择答案片段、不从答案生成候选，也不把别名或推理题判错。

这些类别不是语义准确率：否定句也可能包含答案字面，多会话题可能要组合事实，别名可能不同。缺少独立人工/模型金标准，不报告“新增片段正确率”或“自动审核准确率”。已检查过的数据属于诊断/开发数据，本轮不称作新盲测。

## 冻结与运行

全部代码、用例和说明完成后，先跑新增用例，再治理定向、全量回归，最后跑同一份 LongMemEval cleaned S 全部 500 题。保持原适配器、分母及数据哈希不变，16 workers，临时数据库，0 额外付费模型调用。运行期间不改实现；旧 v8 结果不覆盖。然后离线分别审计 v8 和 v9 的字面覆盖、对齐输入及真实候选数。

```powershell
python scripts/evaluate_longmemeval_governance.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-governance-20261016-v9 --workers 16
python scripts/audit_governance_retention.py --dataset data/longmemeval/longmemeval_s_cleaned.json --run outputs/longmemeval-governance-20261016-v9 --baseline outputs/longmemeval-governance-20261016-v8 --output outputs/longmemeval-governance-quality-20261016-v9
```

完整回放及审计输出只放 Git 忽略的 `outputs/`。Runtime 验收通过真实 reviewer 解析接口、Runtime API、SQLite、人工批准门槛和跨 Agent 召回，模型 HTTP 响应固定，数据库隔离。因此它是集成测试，不冒充三款真实客户端/真实提取模型验收；运行中的客户端尚需用户真实会话确认。
