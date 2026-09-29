"""Build the deterministic, fully synthetic MemWeave cross-agent benchmark."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# Each kernel is an independent synthetic project rule. The benchmark publishes
# ten controlled views of each kernel; splits are by kernel to prevent variants
# of one fact crossing train/dev/test boundaries.
FACTS = [
    ("database", "Use SQLite WAL mode and keep write transactions short.", "SQLite 并发读写使用 WAL，并缩短写事务。", "How should this local app handle concurrent SQLite reads and writes?", "这个本地应用的 SQLite 并发读写怎么处理？"),
    ("database", "Run schema changes inside an explicit transaction and make migrations idempotent.", "数据库结构迁移放在显式事务中，并保证可重复执行。", "What safeguards are required for database migrations?", "数据库迁移要加哪些保护？"),
    ("database", "Use bound parameters for SQL values; do not build queries by concatenating user input.", "SQL 值使用绑定参数，不拼接用户输入构造查询。", "How should user-provided values enter SQL queries?", "用户输入的值应该怎样放进 SQL 查询？"),
    ("database", "Create the FTS index when a knowledge record is written, not by scanning all records during recall.", "知识写入时维护 FTS 索引，不要在召回时扫描全库建索引。", "When should the full-text index be maintained?", "全文索引应该在什么时候维护？"),
    ("database", "Use a bounded connection timeout and report a retryable busy error when SQLite is locked.", "SQLite 锁冲突使用有界等待，并返回可重试的 busy 错误。", "What should the service do when SQLite is temporarily locked?", "SQLite 暂时被锁时服务该怎么处理？"),
    ("retrieval", "Return no memory when evidence is unrelated; zero results are an acceptable outcome.", "证据不相关时返回空结果，零召回是允许的结果。", "Should retrieval force a result when nothing is relevant?", "没有相关知识时检索是否应该硬凑一条？"),
    ("retrieval", "Keep direct matches ahead of expanded candidates; expansion must not displace them.", "直接命中排在扩展候选前面，扩展不能挤掉直接命中。", "How should direct and expanded recall results be ordered?", "直接召回和扩展召回的结果怎么排序？"),
    ("retrieval", "Deduplicate normalized queries before counting repeated shadow hits.", "统计影子命中前，先按规范化问题去重。", "How do we prevent repeated identical prompts inflating shadow-hit counts?", "怎么避免同一个问题反复出现把影子命中次数刷高？"),
    ("retrieval", "Use rare, discriminative terms as anchors; common topic words alone are not enough.", "用稀有且有区分度的词作锚点，不能只凭常见主题词扩展。", "What makes a useful retrieval anchor?", "什么样的词适合作为检索锚点？"),
    ("retrieval", "Answerability requires matching the requested subject and relation, not just the general topic.", "答案充分性要匹配问题主体和关系，不能只看大致主题。", "What must evidence cover before it is injected as an answer?", "把证据注入为答案依据前必须覆盖什么？"),
    ("privacy", "Redact credentials before sending conversation excerpts to a remote model provider.", "对话片段发给远程模型服务前先脱敏凭据。", "What must happen before conversation text goes to a hosted model?", "对话内容发给云端模型前必须做什么？"),
    ("privacy", "Never persist API keys, bearer tokens, or raw passwords in knowledge records.", "知识记录中不得保存 API Key、Bearer Token 或明文密码。", "Which values must never be stored as reusable knowledge?", "哪些内容绝不能作为可复用知识保存？"),
    ("privacy", "Store runtime bearer tokens outside the shared knowledge database and restrict file permissions.", "Runtime Bearer Token 与共享知识库分开保存，并限制文件权限。", "Where should the local runtime token live?", "本地 Runtime Token 应该放在哪里？"),
    ("privacy", "Do not upload real conversation transcripts in a public benchmark; use synthetic examples.", "公开基准不能上传真实会话原文，应使用合成样例。", "What kind of conversations belong in a public benchmark?", "公开基准里应该放哪类会话？"),
    ("privacy", "Treat redaction as best-effort protection, not a guarantee that every secret format is detected.", "脱敏是尽力保护，不能保证识别所有秘密格式。", "Can the redactor guarantee every secret is removed?", "脱敏器能保证所有秘密都被清理吗？"),
    ("lifecycle", "Newly extracted knowledge starts as candidate until it passes the configured review or evidence gate.", "新提炼知识先处于 candidate，经过审核或证据门后才可生效。", "What is the initial state of newly extracted knowledge?", "新提炼出来的知识初始是什么状态？"),
    ("lifecycle", "A normal used signal increases usage evidence but must not reactivate archived knowledge by itself.", "普通 used 信号只能增加使用证据，不能单独让归档知识复活。", "Does one normal use event reactivate an archived record?", "一次普通使用反馈能让归档记录复活吗？"),
    ("lifecycle", "Quarantined knowledge is excluded from normal recall and requires explicit review to restore.", "隔离知识不参加普通召回，恢复前需要显式复核。", "Can quarantined knowledge enter the normal context?", "隔离知识能进入普通上下文吗？"),
    ("lifecycle", "An archived shadow hit is a recovery signal, not proof that restoring the memory improves a task.", "归档影子命中只是恢复信号，不证明恢复后任务效果更好。", "What does a shadow hit prove about task quality?", "影子命中能证明任务质量提升吗？"),
    ("lifecycle", "A superseded version remains available for explicit history questions but must not be treated as current.", "被替代版本可用于明确的历史查询，但不能当成当前事实。", "How should the previous value behave after a rule is superseded?", "规则被替代后旧值应该怎么处理？"),
    ("scope", "Project-scoped knowledge is visible only within the mapped project; user-scoped knowledge is explicitly shared.", "项目级知识只在映射项目内可见；用户级知识才显式跨项目共享。", "How are project-specific memories isolated?", "项目专属记忆如何隔离？"),
    ("scope", "Two agents share project knowledge only when their requests resolve to the same project key.", "两个 Agent 只有解析到同一个 project key 才共享项目知识。", "What must be true for Codex and Claude to share a project rule?", "Codex 和 Claude 要共享项目规则必须满足什么？"),
    ("scope", "Do not map unrelated workspaces to one project merely to increase recall.", "不要为了提高召回把无关工作区映射成同一个项目。", "Should unrelated workspaces share a project mapping to improve recall?", "为了提高召回，是否要把无关工作区设成同一个项目？"),
    ("scope", "An explicit project mapping takes precedence over guessing from a directory name.", "显式项目映射优先于根据目录名猜测项目。", "Which wins: an explicit workspace mapping or a guessed folder name?", "显式工作区映射和猜测目录名哪个优先？"),
    ("scope", "When project identity cannot be resolved, fail closed instead of leaking project memories across workspaces.", "项目身份无法确认时应安全拒绝，不要跨工作区泄漏项目知识。", "What is the safe behavior when the project key is unknown?", "无法确认 project key 时安全的做法是什么？"),
    ("adapter", "Hooks should call the local Runtime at lifecycle events and leave the agent's normal interface unchanged.", "Hook 在客户端生命周期事件调用本地 Runtime，不改变 Agent 原有交互界面。", "How should MemWeave integrate without replacing the agent client?", "不替换 Agent 客户端时 MemWeave 应该怎么接入？"),
    ("adapter", "MCP is an optional external interface; Core and native hooks must work without model-initiated MCP calls.", "MCP 是可选对外接口；Core 和原生 Hook 不依赖模型主动调用 MCP。", "Is MCP required for automatic memory recall?", "自动记忆召回是否依赖 MCP？"),
    ("adapter", "The recall hook should fail open with empty output if the local Runtime is unavailable.", "本地 Runtime 不可用时，召回 Hook 应快速降级为空输出。", "What should the recall hook do if the local service is down?", "本地服务挂了时召回 Hook 应该怎么做？"),
    ("adapter", "The end-of-turn hook should enqueue bounded work rather than block the agent on model extraction.", "回合结束 Hook 应提交有界后台任务，不应阻塞 Agent 等模型提炼。", "Should end-of-turn learning wait synchronously for extraction?", "回合结束时学习流程应该同步等模型提炼吗？"),
    ("adapter", "Install hooks only for agents explicitly enabled by the user, and preserve unrelated hook configuration.", "仅为用户明确启用的 Agent 安装 Hook，并保留无关 Hook 配置。", "What is the safe rule for changing an agent's hook configuration?", "修改 Agent Hook 配置的安全原则是什么？"),
    ("testing", "Test both positive traffic and negative traffic; compilation success alone does not prove recognition.", "正流量和负流量都要测；编译成功本身不证明识别正确。", "What evidence is needed beyond a successful rule compile?", "规则编译成功以外还需要什么证据？"),
    ("testing", "Use paired baseline and treatment tasks with the same prompts and model settings to estimate framework impact.", "用相同问题和模型设置做基线/启用框架配对任务，估计框架影响。", "How should task success impact be compared fairly?", "怎样公平比较任务成功率的影响？"),
    ("testing", "Report abstention errors separately from missed relevant memories.", "拒答错误和漏召回相关知识要分开报告。", "Which two retrieval failures should not be merged into one score?", "哪两类检索错误不应该混成一个分数？"),
    ("testing", "Keep a held-out test split grouped by underlying fact so paraphrases cannot leak across splits.", "测试集按底层事实分组留出，避免同一事实的改写题跨集合泄漏。", "How should paraphrase variants be split between development and test?", "同一事实的改写题应该怎样分到开发集和测试集？"),
    ("testing", "Synthetic retrieval success does not establish real-agent task improvement.", "合成检索题通过不等于真实 Agent 任务效果提升。", "What can a synthetic retrieval benchmark not establish by itself?", "合成检索基准本身不能证明什么？"),
    ("performance", "Measure the incremental hook cost with framework-on and framework-off paired runs.", "用框架开关配对运行测量 Hook 带来的额外耗时。", "How should MemWeave's added latency be measured?", "如何测 MemWeave 增加的延迟？"),
    ("performance", "Keep embedding models and cross-encoders off the hot path unless measured gains justify their cost.", "除非实测收益足以抵成本，否则不要把 embedding 或 cross-encoder 放进热路径。", "When should a heavy reranker be added to recall?", "什么时候才应该给召回加重型重排器？"),
    ("performance", "Bound the number of injected records and omit lower-confidence candidates when the context budget is full.", "限制注入条数；上下文预算满时省略低置信候选。", "What should happen when the memory context budget is exhausted?", "记忆上下文预算用完时怎么办？"),
    ("performance", "Move document-frequency measurements to writes or cache them with write-time invalidation.", "文档频率统计放在写路径，或缓存并在写入时失效。", "Where should expensive document-frequency work happen?", "较重的文档频率统计应该放在哪条路径？"),
    ("performance", "Use bounded queues and explicit retry limits for background learning jobs.", "后台学习任务使用有界队列和明确的重试上限。", "How do we prevent a learning queue from growing without limit?", "怎样防止学习队列无限增长？"),
    ("learning", "Store the smallest reusable rule with its scope, source, and evidence instead of copying a raw transcript.", "保存最小可复用规则及其范围、来源和证据，不复制原始对话。", "What should be persisted from a useful conversation?", "有用对话应该保存什么？"),
    ("learning", "A question, hypothesis, or unverified assistant guess is not a confirmed user preference.", "问题、假设或未验证的助手猜测都不是已确认的用户偏好。", "Can an assistant's unverified guess become an established preference?", "助手未经验证的猜测能变成确定偏好吗？"),
    ("learning", "A stable preference can be considered for automatic acceptance only when the user clearly states it as persistent.", "只有用户明确表达长期偏好的稳定偏好才可考虑自动采纳。", "What evidence is needed before auto-accepting a preference?", "自动采纳偏好前需要什么证据？"),
    ("learning", "Repeated extraction of the same source conversation is not independent confirmation.", "同一来源会话被重复提炼不算独立确认。", "Does extracting one turn twice create two confirmations?", "同一轮对话提炼两次算两份确认吗？"),
    ("learning", "When the adapter cannot inspect the complete turn, mark evidence as unknown rather than claiming full coverage.", "Adapter 看不到完整轮次时，应标记证据未知，不宣称已完整覆盖。", "How should incomplete transcript visibility be represented?", "看不到完整 transcript 时证据状态怎么标？"),
    ("governance", "Archive based on explicit lifecycle evidence and keep recovery separate from permanent deletion.", "依据明确生命周期证据归档，并将恢复与永久删除分开。", "How does archiving differ from permanent deletion?", "归档和永久删除有什么区别？"),
    ("governance", "A knowledge item replaced by a newer conflicting value must not be resurrected by ordinary hit counts.", "被新版冲突值替代的知识不能因普通命中次数而复活。", "Can LFHV restore a value that has been explicitly superseded?", "被明确替代的值能被 LFHV 恢复吗？"),
    ("governance", "Do not treat lack of recent use as proof that a knowledge item is false.", "近期没有使用不能证明一条知识是错误的。", "Does inactivity prove a memory is incorrect?", "知识长期没用能证明它是错的吗？"),
    ("governance", "Use separate measurements for recall opportunity, actual context emission, and downstream task success.", "分别统计召回机会、实际上下文注入和下游任务成功。", "Which stages need separate evidence in a reuse claim?", "证明知识复用时哪些环节要分开取证？"),
    ("governance", "Keep a bounded audit trail and define retention for event tables as well as active knowledge.", "审计记录要有界；事件表和活跃知识都需要保留策略。", "What can still grow even if the active knowledge set is capped?", "即使限制了活跃知识，哪些数据仍可能持续增长？"),
]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_for(index: int) -> str:
    return "test" if index >= 20 else ("dev" if index >= 10 else "calibration")


def make_case(fact_index: int, variant: int, fact: tuple[str, str, str, str, str]) -> dict:
    topic, en, zh, q_en, q_zh = fact
    kernel = f"kernel-{fact_index + 1:03d}"
    case_id = f"MWX-{fact_index + 1:03d}-{variant + 1:02d}"
    source = "claude-code" if (fact_index + variant) % 2 == 0 else "codex"
    target = "codex" if source == "claude-code" else "claude-code"
    project = f"project-{(fact_index % 5) + 1}"
    fact_id = f"{case_id}-memory-1"
    records = []
    query = q_en if variant % 2 == 0 else q_zh
    decision = "inject"
    gold = [fact_id]
    forbidden: list[str] = []
    answer = en if variant % 2 == 0 else zh
    category = "cross_agent_reuse"
    probe = "none"

    if variant == 0:  # paraphrase, cross-client wording
        category = "cross_agent_recall"
        query = f"另一个 Agent 在 {project} 留了条规则：{q_zh} 请告诉我原来的结论。" if variant % 2 else f"A different agent left a rule in {project}. {q_en} What was the agreed guidance?"
        records = [{"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "active", "source_session": f"{kernel}-session-a", "content": en}]
    elif variant == 1:  # explicit cross-language gap
        category = "cross_language_recall"
        query = q_zh if source == "codex" else q_en
        records = [{"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "active", "source_session": f"{kernel}-session-a", "content": en if source == "codex" else zh}]
        answer = zh if query == q_zh else en
    elif variant == 2:  # add a lexically similar but wrong project decoy
        category = "distractor_ranking"
        decoy_id = f"{case_id}-memory-decoy"
        decoy = {"id": decoy_id, "source_agent": target, "scope": "project", "project_key": project,
                 "status": "active", "source_session": f"{kernel}-session-decoy",
                 "content": f"For {topic}, this separate synthetic note is only a discussion topic and gives no additional rule."}
        records = [{"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "active", "source_session": f"{kernel}-session-a", "content": en}, decoy]
        query = f"For {project}, {q_en} Ignore notes that only mention {topic} without a rule."
    elif variant == 3:  # same record but wrong workspace context
        category = "project_scope_isolation"
        query = f"在 project-{((fact_index + 1) % 5) + 1} 里：{q_zh}"
        records = [{"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "active", "source_session": f"{kernel}-session-a", "content": en}]
        decision, gold, answer = "abstain", [], ""
        forbidden = [fact_id]
    elif variant == 4:  # superseded old value must lose to current value
        category = "knowledge_update"
        old_id, current_id = f"{case_id}-memory-old", f"{case_id}-memory-current"
        old = {"id": old_id, "source_agent": source, "scope": "project", "project_key": project,
               "status": "archived", "superseded_by": current_id,
               "source_session": f"{kernel}-session-old", "content": f"Old rule: {en}"}
        current = {"id": current_id, "source_agent": target, "scope": "project", "project_key": project,
                   "status": "active", "source_session": f"{kernel}-session-current", "content": f"Updated rule: {zh}"}
        records = [old, current]
        query, decision, gold, forbidden, answer = f"在 {project} 中，当前规则是什么？{q_zh}", "inject", [current_id], [old_id], zh
    elif variant == 5:  # same-session noise must not be mistaken for evidence
        category = "same_session_noise_filter"
        note_id = f"{case_id}-memory-note"
        records = [
            {"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
             "status": "active", "source_session": f"{kernel}-session-multi", "content": en},
            {"id": note_id, "source_agent": source, "scope": "project", "project_key": project,
             "status": "active", "source_session": f"{kernel}-session-multi", "content": f"The team discussed {topic}; no separate decision was recorded in this note."},
        ]
        query = f"Using the shared notes for {project}, {q_en}"
        forbidden = [note_id]
    elif variant == 6:  # query asks for support absent from otherwise related memory
        category = "answerability_abstention"
        records = [{"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "active", "source_session": f"{kernel}-session-a", "content": en}]
        query = f"For {project}, which benchmark result proves that {topic} improves real-world task success by at least 20 percent?"
        decision, gold, answer, forbidden = "abstain", [], "", [fact_id]
    elif variant == 7:  # lexical decoy and unsupported detail
        category = "near_miss_abstention"
        decoy_id = f"{case_id}-memory-decoy"
        records = [{"id": decoy_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "active", "source_session": f"{kernel}-session-decoy", "content": f"The project discussed {topic}, but no decision or measured result was recorded."}]
        query = f"For {project}, what exact numeric threshold did the project approve for {topic}?"
        decision, gold, answer, forbidden = "abstain", [], "", [decoy_id]
    elif variant == 8:  # archived only: shadow probe sees it, normal recall cannot emit it
        category = "lfhv_shadow_recovery"
        records = [{"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                    "status": "archived", "source_session": f"{kernel}-session-a", "content": en}]
        query = f"For {project}, consult any archived note that could answer this: {q_en}"
        decision, gold, answer, forbidden, probe = "abstain", [], "", [fact_id], "shadow_candidate"
    else:  # non-recoverable lifecycle states
        category = "lifecycle_exclusion"
        status = "quarantined" if fact_index % 2 == 0 else "archived"
        record = {"id": fact_id, "source_agent": source, "scope": "project", "project_key": project,
                  "status": status, "source_session": f"{kernel}-session-a", "content": en}
        if status == "archived":
            record["superseded_by"] = f"{case_id}-newer-version"
        records = [record]
        query = f"For {project}, using only currently approved knowledge, answer this: {q_en}"
        decision, gold, answer, forbidden, probe = "abstain", [], "", [fact_id], "must_not_recover"

    return {
        "case_id": case_id,
        "kernel_id": kernel,
        "split": split_for(fact_index),
        "category": category,
        "source_agent": source,
        "target_agent": target,
        "project_context": project if variant != 3 else f"project-{((fact_index + 1) % 5) + 1}",
        "query": query,
        "memory_records": records,
        "expected": {
            "decision": decision,
            "evidence_ids": gold,
            "forbidden_ids": forbidden,
            "answer": answer,
            "answer_rubric": (f"回答必须准确表达这条规则，不得增加与证据冲突的条件：{answer}" if decision == "inject"
                              else "明确说明现有知识不足以支持该问题；不得猜测或引用被禁止的知识。"),
            "shadow_probe": probe,
        },
        "synthetic": True,
    }


def main() -> None:
    if len(FACTS) != 50:
        raise SystemExit(f"Expected 50 fact kernels; found {len(FACTS)}")
    rows = [make_case(i, variant, fact) for i, fact in enumerate(FACTS) for variant in range(10)]
    (ROOT / "cases.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"cases": len(rows), "kernels": len(FACTS), "sha256": digest((ROOT / "cases.jsonl").read_text(encoding="utf-8"))}, indent=2))


if __name__ == "__main__":
    main()
