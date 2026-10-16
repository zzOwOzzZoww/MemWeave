# v10：独立子句的证据边界与去重查询成本

## 修复范围

v9 留下的明确问题是：即使 reviewer 只引用一个确定事实，`quote_context` 仍可能把同句的计划、求助或推测补回；whole-turn 兜底也没有安全的子句边界。不能通过删除不确定性、时效、条件和范围检查解决。

本轮沿用现有 `source_quotes`，在同一次 reviewer 调用中明确要求按独立事实分别提案，不新增模型调用、不信任模型的真值或批准声明。Core 仅为两侧都有主体/谓语的结构性并列建立边界；关系从句、无主体续句、括号、引文、条件、前置不确定性与时效不能被当成独立事实。上下文索引只在当前提案内复用，不全局缓存私人对话。存储仍是原文片段，不用模型改写补出不存在的完整事实。

v10 完整复测后发现主体判断过宽及跨句缩窄回归，v10.1 收紧显式主语，列表项、共用主语动作和介词片段不独立切开；新增 durable 混合缩窄仅针对同一句的结构性并列，不因长消息任意位置存在计划就替换原有整条准入。v10 原结果保留，v10.1 使用新输出目录重新冻结及完整运行，不覆盖前次结果。

v10.1 在 107/500 题时发现 `I think` 等认识性框架仍能在右侧形成独立切点，已停止该轮并标记 INCOMPLETE，不能作为全量成绩。v10.2 在两侧均保留这种框架，不用它触发新的 durable 缩窄；重新回归、冻结并完整重放。只有包含 report.json 且通过完整性核验的目录才算完成。

截掉完整上下文的条件、归属或意图框架仍拒绝；未拆清的嵌套事实仍保守处理。该能力不是通用语义解析，不承诺从计划中推导已有拥有关系或稳定偏好。独立片段不继承整轮自动批准或全局范围。

并列切分不能把“我不确定 / 这只是临时的”当成另一件独立事实；这些限定仍与原断言一起检查。除上下文检查外，入库前也比较明确否定与适用限制标记，防止正文裁掉 `not / only in tests / 不用于生产` 等内容。完整有限制的事实仍可待审，临时或不确定内容保留原有分类与显式长期保留例外；项目范围限定继续由原有范围元数据保护。这不是对任意语义改写的完整检测。

候选正文复用增加部分表达式索引，按项目、范围、类型、正文长度/前128字符、说话人查找，再比较完整正文和候选状态、替代及过期条件。前缀不是身份，不足以合并；不新增整段正文索引或批量改写历史记录，不改手动发布身份。

回归同时覆盖前缀碰撞、中文、NUL、重开建索引和真实查询计划；旧表并发迁移测试先移除新版本索引再模拟旧表，迁移后检查索引重建及原文、状态不变。

## 验证与冻结

先全部实现和用例完成，再运行失败/新增范围、治理定向及全量回归，最后完整重放同一份 cleaned S 500题。沿用原适配器和分母，不按公开标准答案选择子句；已见数据只作诊断，不称新盲测。16 workers，临时数据库，0 模型调用；运行中不改冻结源码，不覆盖 v8/v9。

```powershell
python scripts/evaluate_longmemeval_governance.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-governance-20261016-v10 --workers 16
python scripts/audit_governance_retention.py --dataset data/longmemeval/longmemeval_s_cleaned.json --run outputs/longmemeval-governance-20261016-v10 --baseline outputs/longmemeval-governance-20261016-v9 --output outputs/longmemeval-governance-quality-20261016-v10
python scripts/evaluate_longmemeval_governance.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-governance-20261016-v10-1 --workers 16
python scripts/audit_governance_retention.py --dataset data/longmemeval/longmemeval_s_cleaned.json --run outputs/longmemeval-governance-20261016-v10-1 --baseline outputs/longmemeval-governance-20261016-v9 --output outputs/longmemeval-governance-quality-20261016-v10-1
python scripts/evaluate_longmemeval_governance.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-governance-20261016-v10-2 --workers 16
python scripts/audit_governance_retention.py --dataset data/longmemeval/longmemeval_s_cleaned.json --run outputs/longmemeval-governance-20261016-v10-2 --baseline outputs/longmemeval-governance-20261016-v9 --output outputs/longmemeval-governance-quality-20261016-v10-2
```

报告全部标注消息的 full/partial/none、答案字面留存与丢失、未知项、唯一候选、自动晋升和并发耗时。不将字面匹配当作语义正确，不把候选数下降当作治理精度上升。

另用固定技术合成输入做前后本地性能诊断（120次learn、2万候选表30次精确复用），不使用公开答案、不测检索。短时单进程测量不代替并发完整回放，不做生产加速比例承诺。reviewer固定HTTP响应只验证契约和解析链路，不证明真实提取模型理解了所有复杂句。

本轮不部署生产 Runtime、不迁移现有知识、不改 Hook/适配器/检索/LFHV，不上传 GitHub；原文和逐项诊断仅放 Git 忽略的 outputs。
