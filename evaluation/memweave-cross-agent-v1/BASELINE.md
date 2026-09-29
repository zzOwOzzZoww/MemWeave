# v1 候选集基线结果

> 2026-09-29 修改备注：以下数字为首次运行的历史结果，本轮没有重新测试。源码已增加中英文术语统一、正文索引、答案类型门禁与 LFHV 当轮复用；新效果待测。旧 Runner 实际共用一个临时数据库、以逐例项目键隔离，并统一使用共享 Claude Adapter 实现；下文“每例临时数据库”表述不准确。新 Runner 已改为每例独立数据库、按目标使用真实 Adapter，并移除了场景标签标题，因此新旧测量必须注明 Runner 版本。

运行日期：2026-09-29。数据集：`mw-cross-agent-v1.0.0-candidate`。代码：本轮修改前的 MemWeave Runtime。使用 300 条按 kernel 留出的 `test` split，通过本地 Adapter/SQLite 检索执行；当时每个 case 使用独立临时项目空间，共用一个临时数据库。未调用模型或付费 API。

| 指标 | 首轮结果 |
|---|---:|
| 案例数 | 300 |
| 决策准确率 | 60.0% |
| 正向证据召回率 | 30.7% |
| 应拒答题净空率 | 78.7% |
| 禁止证据注入率 | 11.0% |
| LFHV 影子候选发现率 | 43.3% |
| Adapter P50 / P95 | 18.3 / 28.0 ms |
| 下游答案正确率 / 任务成功率 | 未测量 |

需要优先复核和改进的类别：跨语言召回 3.3%、知识更新后当前值召回 13.3%、近似主题但问题要求证据不支持时的净空率 0.0%。项目隔离、已归档/隔离/替代记录的正常注入阻断在这组受控题上为 100%；这只是该合成集合上的结果。

这是一组**首次诊断基线**，不是产品质量证明，也不是经过独立标注员复核的最终公开成绩。数据由规则化合成场景构成，问题模板和内容复杂度低于真实多轮会话；公开前需人工复核全部答案/拒答边界。不要针对 `test` split 调参；修改系统后应把此结果保留为原始基线，并另建新版本的实验记录。

复现：

```powershell
python evaluation/memweave-cross-agent-v1/validate_dataset.py
python evaluation/memweave-cross-agent-v1/run_memweave.py --split test --output outputs/mw-cross-agent-test-run
python evaluation/memweave-cross-agent-v1/score_results.py --predictions outputs/mw-cross-agent-test-run/predictions.jsonl --split test --output outputs/mw-cross-agent-test-run/report.json
```
