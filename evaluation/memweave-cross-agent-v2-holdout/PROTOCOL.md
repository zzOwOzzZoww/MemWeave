# MemWeave Cross-Agent Holdout v2

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Date: 2026-10-08
- Verification Status: UNVERIFIED；数据/工具已准备，dev 预检中断，test 未运行，无有效成绩。
- Label Author: Codex 助手；未经过独立人工标注复核。

## 目的与数据

使用未参与过生产规则调参的新主题，检查跨 Agent 的证据复用、跨语言召回、证据不足时的误注入和范围/版本隔离。不新增 Agent 接入，不改变 Core 或 Hook。

20 个合成主题各覆盖 6 种场景，共 120 行；按主题确定性切分为 dev 5 主题 / 30 行、test 15 主题 / 90 行。全新主题涉及格式处理决策，例如 Parquet 字典编码、GeoJSON 坐标顺序、WebVTT 时间戳精度、EPUB 阅读方向。记录中的取值是虚构项目决策，不是格式使用建议。

每主题覆盖跨语言、直接事实、缺少所问细节、近似主体干扰、版本更新、错误项目隔离。证据不足例轮换覆盖缺少关系、对象上下文不同、缺少历史时间三个子类。直接事实、加干扰项和换项目可使用相同问题文本以构成受控对照，但它们始终在同一主题 split 内。

没有真实私人会话，也没有复制 LoCoMo、LongMemEval 或 Cross-Agent v1 的知识内容。精确文本复用校验不等于保证语义完全独立；本集仍为同一开发助手编写的规则化模板，不能称作第三方盲测。

## 文件与标注

- `build_dataset.py`：确定性数据生成与完整性校验；拒绝覆盖冻结数据。
- `cases.jsonl`：120 个公开合成场景。
- `dataset-card.json`：SHA-256、主题 split、场景数量、标注来源。
- `evaluate.py`：独立冻结/执行器及证据评分；复用现有隔离夹具辅助函数，不改变生产检索。

数据 SHA-256：`19f71cfbe33cbd5392b227abf52979030fd0d86d7d60f868e91b39ccc7670a24`。

每条记录的 `fact` 标签包含主体、关系、对象和有效期；每个请求包含主体、关系、对象上下文及 as-of 时间。`expected` 分别声明必需证据、禁止证据与主题相关背景。正例必须找到必需证据，主题相似但关系/对象不符不算正确；版本更新不接受明确被替代的旧值；错误项目不能注入完全匹配的记录。

时间标签供评分，不凭空赋予 Core 通用时间推理能力。`subject_terms` 来源于记录主体，不读取问题或答案；时间/新旧值以原内容与生命周期状态体现。主体、关系和答案标签不会传入查询重写或排序。

## 固定执行口径

每例独立临时 SQLite 数据库；source/target 为现有 Claude Code 与 Codex Adapter。记录通过明确测试证据激活，不评测自动抽取或准入。每次重复从相同 seed 克隆，完整系统运行，limit=3、slack=2、上下文预算 4,000 字符，每例重复 2 次，固定夹具时钟。

保存三个不同层次：原始 FTS 候选、通过 Core 门禁的返回证据、Adapter 实际注入。诊断保存阶段计数、原因与稳定证据 ID；重复校验排除耗时和内部随机 ID。源码、数据、协议、辅助脚本与环境冻结为清单；不一致拒绝运行，不静默重新冻结 test。

分别报告正例证据召回、完整且干净率、负例拒答净空率、全部案例中的禁止证据注入率、证据不足例的主题相关候选覆盖，以及各类别/各主题结果。错误类别区分主体、关系、对象上下文、时间、过期值和项目。不报告官方 QA 准确率、模型 Token 节省、显著性、TTFT 或真实任务收益；15 个 test 主题不能当成 90 个独立样本。

## 当前状态

数据已生成并校验。首次 dev 预检在第一例的第二次重复中断：新评测器误将嵌套的阶段 `elapsed_ms` 纳入语义比较；并非 MemWeave 输出已确认不一致。修正只排除耗时，不调整标签或生产规则，新增单元测试覆盖这个问题与观测透明性。

原始冻结清单及失败预测保留在本地 `outputs/mw-cross-agent-v2-holdout-freeze-20261008/` 和 `outputs/mw-cross-agent-v2-holdout-dev-20261008/`。**没有自动重试，没有有效 dev/test 成绩，test 尚未运行。** 失败后源码已变更，因此旧冻结清单应拒绝运行。下一次明确授权后，创建新的冻结清单和输出目录，不覆盖原始产物。

## 下一次命令

仓库根目录下执行；无模型调用，无外部服务，无私人数据库访问。实验硬预算 1,800 秒；进程异常时不自动重试。先确认以下命令，再执行：

```powershell
python evaluation/memweave-cross-agent-v2-holdout/build_dataset.py --check
python evaluation/memweave-cross-agent-v2-holdout/evaluate.py freeze --output outputs/mw-holdout-v2-freeze-r2
python evaluation/memweave-cross-agent-v2-holdout/evaluate.py run --manifest outputs/mw-holdout-v2-freeze-r2/manifest.json --split dev --output outputs/mw-holdout-v2-dev-r2
python evaluation/memweave-cross-agent-v2-holdout/evaluate.py run --manifest outputs/mw-holdout-v2-freeze-r2/manifest.json --split test --output outputs/mw-holdout-v2-test-r2
```

dev 成绩不理想不能成为修改 test 标签的理由。test 首次运行后，任何依其失败进行的生产调参须使用新的主题集验证；固定版本上重复运行用于确定性验证不算独立泛化验证。完整预测只在本地 `outputs/`，不自动上传 GitHub。
