"""Bounded bilingual terminology shared by retrieval and evidence checks.

These are lexical equivalents, not inferred facts (database never implies WAL).
They run locally and never load an embedding model or call a translation API.
"""
from functools import lru_cache
import re


WORD = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:[_.:-][A-Za-z0-9]+)*|[一-鿿]{2,}')
CONCEPT_TOKEN_PREFIX = 'mwconcept_'
# Explicit inflections avoid corrupting identifiers, proper names and acronyms.
_INFLECTIONS = {
    'reads': 'read', 'reading': 'read', 'reader': 'read', 'readers': 'read',
    'writes': 'write', 'writing': 'write', 'writer': 'write', 'writers': 'write',
    'queries': 'query', 'questions': 'question', 'results': 'result',
    'requests': 'request', 'responses': 'response', 'records': 'record',
    'transactions': 'transaction', 'migrations': 'migration', 'parameters': 'parameter',
    'indexes': 'index', 'indices': 'index', 'prompts': 'prompt', 'counts': 'count',
    'anchors': 'anchor', 'terms': 'term', 'tokens': 'token', 'timeouts': 'timeout',
    'matches': 'match', 'candidates': 'candidate', 'rules': 'rule', 'decisions': 'decision',
    'retries': 'retry', 'errors': 'error', 'versions': 'version', 'sources': 'source',
    'files': 'file', 'paths': 'path', 'permissions': 'permission',
    'stored': 'store', 'storing': 'store', 'stores': 'store',
    'conversations': 'conversation', 'models': 'model', 'benchmarks': 'benchmark',
    'secrets': 'secret', 'guarantees': 'guarantee', 'extracted': 'extract',
    'archived': 'archive', 'restored': 'restore', 'restoring': 'restore',
    'reactivated': 'reactivate', 'superseded': 'supersede',
    'improved': 'improve', 'improves': 'improve', 'tasks': 'task',
    'agents': 'agent', 'clients': 'client', 'hooks': 'hook',
    'workspaces': 'workspace', 'mappings': 'mapping', 'mapped': 'mapping',
    'queues': 'queue', 'rerankers': 'reranker', 'comparisons': 'comparison',
    'paired': 'pair', 'pairing': 'pair', 'paraphrases': 'paraphrase',
    'confirmations': 'confirmation', 'confirmed': 'confirmation',
    'deletions': 'deletion', 'deleted': 'deletion', 'deleting': 'deletion',
    'measurements': 'measurement', 'measured': 'measurement',
    'emissions': 'emission', 'emitted': 'emission',
}


def normalize_word(word: str) -> str:
    value = word.casefold()
    return _INFLECTIONS.get(value, value)


# One concept contributes at most one evidence unit, regardless of how many
# synonymous surface forms appear. The same table supplies bounded FTS rewrites.
_CONCEPTS = (
    ('database', r'数据库|表结构|数据库结构|\bdatabases?\b|\bschema\b', ('database', '数据库', 'schema', '表结构')),
    ('concurrency', r'并发|\bconcurrent(?:ly)?\b|\bconcurrency\b', ('concurrent', 'concurrency', '并发')),
    ('read', r'读取|读写|\bread(?:s|ing|ers?)?\b', ('read', 'reads', 'reader', 'readers')),
    ('write', r'写入|读写|\bwrit(?:e|es|ing|ers?)\b', ('write', 'writes', 'writer', 'writers')),
    ('transaction', r'事务|\btransactions?\b', ('transaction', 'transactions', '事务')),
    ('migration', r'迁移|\bmigrations?\b', ('migration', 'migrations', '迁移')),
    ('query', r'查询|\bquer(?:y|ies)\b', ('query', 'queries', '查询')),
    ('parameter', r'参数|\bparameters?\b', ('parameter', 'parameters', '参数')),
    ('index', r'索引|\bindex(?:es)?\b|\bindices\b', ('index', '索引')),
    ('fulltext', r'全文|\bfull[- ]text\b|\bfts5?\b', ('fts', 'fts5', 'full-text', '全文')),
    ('lock', r'锁定|被锁|锁住|锁冲突|\blocked\b|\blocking\b', ('locked', '被锁', 'busy', '锁冲突')),
    ('timeout', r'超时|\btimeouts?\b', ('timeout', '超时')),
    ('retry', r'重试|\bretr(?:y|ies)\b', ('retry', '重试')),
    ('retrieval', r'检索|召回|\bretrieval\b|\brecall\b', ('retrieval', 'recall', '检索', '召回')),
    ('result', r'结果|\bresults?\b', ('result', '结果', 'results', '空结果')),
    ('evidence', r'证据|\bevidence\b', ('evidence', '证据')),
    ('relevance', r'相关|无关|不相关|\brelevance\b|\brelevant\b|\bunrelated\b|\birrelevant\b', ('relevant', '相关', 'unrelated', '不相关')),
    ('direct', r'直接|\bdirect\b', ('direct', '直接')),
    ('expansion', r'扩展|\bexpan(?:ded|sion)\b', ('expanded', 'expansion', '扩展')),
    ('shadow', r'影子|\bshadow\b', ('shadow', '影子')),
    ('deduplication', r'去重|重复|相同|\bdeduplicat(?:e|ion)\b|\bduplicates?\b|\brepeated\b|\bidentical\b', ('deduplicate', '去重', 'duplicate', '重复')),
    ('anchor', r'锚点|检索锚|\banchors?\b', ('anchor', 'anchors', '锚点')),
    ('discrimination', r'有用|有效|稀有|区分性|鉴别性|\buseful\b|\brare\b|\bdiscriminative\b|\bdistinctive\b', ('discriminative', '区分性', 'rare', '稀有')),
    ('term', r'术语|词语|词|\bterms?\b', ('term', '词', 'terms', '术语')),
    ('match', r'匹配|覆盖|\bmatch(?:es|ed|ing)?\b|\bcovers?\b', ('match', '匹配', 'cover', '覆盖')),
    ('answer', r'答案|回答|答案充分性|\banswers?\b|\banswerability\b', ('answer', '答案', 'answerability', '充分性')),
    ('subject', r'主体|\bsubjects?\b', ('subject', '主体')),
    ('relation', r'关系|\brelations?\b|\bpredicates?\b', ('relation', 'predicate', '关系')),
    ('answerability', r'答案充分性|\banswerability\b', ('answerability', '充分性')),
    ('encoding', r'编码|\bencoding\b', ('encoding', '编码')),
    ('conversation', r'对话|会话|会话原文|\bconversations?\b|\btranscripts?\b', ('conversation', '对话', 'transcript', '会话')),
    ('remote', r'云端|远程|\bremote\b|\bcloud\b', ('remote', '远程', 'cloud', '云端')),
    ('model', r'模型|服务商|\bmodels?\b|\bproviders?\b', ('model', '模型', 'provider', '服务商')),
    ('knowledge', r'知识|记忆|\bknowledge\b|\bmemory\b', ('knowledge', '知识', 'memory', '记忆')),
    ('storage', r'保存|存储|持久化|\bstor(?:e|es|ed|ing)\b|\bpersist(?:s|ed|ing|ence)?\b', ('store', '保存', 'persist', '存储')),
    ('benchmark', r'基准|\bbenchmarks?\b', ('benchmark', '基准', 'benchmarks', '公开基准')),
    ('public', r'公开|公共|\bpublic\b', ('public', '公开')),
    ('redaction', r'脱敏|清理|\bredact(?:s|ed|ing|ion)?\b', ('redact', '脱敏', 'redaction', '清理')),
    ('credential', r'凭据|秘密|密钥|口令|密码|\bcredentials?\b|\bsecrets?\b|\bpasswords?\b|\bapi[ -]?keys?\b|\bbearer tokens?\b', ('credential', '凭据', 'secret', '秘密')),
    ('guarantee', r'保证|确保|\bguarantees?\b|\bguaranteed\b', ('guarantee', '保证', 'guaranteed', '确保')),
    ('extraction', r'提炼|抽取|\bextract(?:s|ed|ing|ion)?\b', ('extract', '提炼', 'extracted', '抽取')),
    ('state', r'状态|处于|\bstates?\b|\binitial\b', ('state', '状态', 'initial', '处于')),
    ('feedback', r'反馈|信号|\bfeedback\b|\bsignals?\b', ('feedback', '反馈', 'signal', '信号')),
    ('usage', r'使用|采用|\busage\b|\bused\b|\buse\b', ('usage', '使用', 'used', '采用')),
    ('archive', r'归档|\barchiv(?:e|es|ed|ing)\b', ('archive', '归档', 'archived', '已归档')),
    ('recovery', r'恢复|复活|重新激活|\brestor(?:e|es|ed|ing|ation)\b|\breactivat(?:e|es|ed|ing|ion)\b|\bresurrect(?:s|ed|ing|ion)?\b', ('restore', '恢复', 'reactivate', '复活')),
    ('isolation', r'隔离|\bisolat(?:e|es|ed|ing|ion)\b|\bquarantin(?:e|es|ed|ing)\b', ('isolation', '隔离', 'quarantine', '已隔离')),
    ('proof', r'证明|证实|\bproof\b|\bprove[sd]?\b|\bestablish(?:es|ed|ing)?\b', ('proof', '证明', 'prove', '证实')),
    ('task', r'任务|\btasks?\b', ('task', '任务')),
    ('improvement', r'提升|改善|收益|\bimprov(?:e|es|ed|ing|ement)\b|\bgains?\b', ('improve', '提升', 'improvement', '改善')),
    ('supersession', r'替代|取代|\bsupersed(?:e|es|ed|ing)\b|\breplac(?:e|es|ed|ing)\b', ('supersede', '替代', 'superseded', '被替代')),
    ('history', r'历史|旧值|旧版本|\bhistory\b|\bhistorical\b|\bprevious\b', ('history', '历史', 'previous', '旧值')),
    ('budget', r'预算|\bbudgets?\b', ('budget', '预算')),
    ('context', r'上下文|\bcontext\b', ('context', '上下文')),
    ('version', r'版本|\bversions?\b', ('version', '版本')),
    ('threshold', r'阈值|\bthresholds?\b', ('threshold', '阈值')),
    # Project/runtime concepts are deliberately phrase-bounded.  They close
    # common Chinese/English vocabulary gaps without turning generic words such
    # as ``project`` or ``service`` into universal matches.
    ('project_scope', r'项目(?:级|专属|范围|作用域)|映射项目|\bproject[- ]scoped\b|\bproject[- ]specific\b|\bproject scope\b|\bmapped project\b', ('project scope', '项目作用域', 'project-scoped', '项目专属')),
    ('project_identity', r'项目(?:标识|身份|映射)|工作区映射|目录名猜测|\bproject[ _-]?key\b|\bproject identity\b|\bworkspace mapping\b|\bguessed folder name\b', ('project key', '项目映射', 'project identity', '工作区映射')),
    ('agent', r'\bagents?\b|智能体|两个 Agent|Codex.{0,20}Claude|Claude.{0,20}Codex', ('agent', '智能体', 'Codex', 'Claude')),
    ('sharing', r'共享|共用|\bshar(?:e|es|ed|ing)\b', ('share', 'shared', '共享')),
    ('fail_closed', r'失败关闭|安全失败|保守拒绝|无法确认.{0,24}(?:拒绝|隔离|泄漏)|\bfail(?:s|ed|ing)? closed\b|\bfail-closed\b|\bcannot be resolved\b.{0,45}\b(?:leak|isolat)', ('fail closed', '安全失败', '拒绝泄漏')),
    ('hook_runtime', r'召回钩子|生命周期钩子|本地 Runtime|客户端生命周期|\b(?:native |recall |learning )?hooks?\b|\blocal runtime\b|\bclient lifecycle\b', ('hook', '钩子', 'local runtime', '本地 Runtime')),
    ('integration', r'接入|集成|替换客户端|交互界面|\bintegrat(?:e|es|ed|ing|ion)\b|\breplac(?:e|es|ed|ing) the (?:agent )?client\b|\binteraction interface\b', ('integrate', '接入', 'client', '客户端')),
    ('compilation', r'编译|构建通过|\bcompil(?:e|es|ed|ing|ation)\b|\bbuild succeeds?\b', ('compile', '编译', 'compilation')),
    ('positive_negative_test', r'正负(?:样本|流量|测试)|正例.{0,12}负例|\bpositive (?:and|/) negative (?:traffic|tests?|cases?)\b', ('positive negative test', '正负测试', 'positive traffic', 'negative traffic')),
    ('paired_evaluation', r'配对(?:运行|任务|对照|比较)|框架开关对照|公平(?:对比|比较)|相同.{0,18}(?:问题|模型).{0,18}(?:对比|比较)|\bpaired (?:runs?|tasks?|comparison|evaluation)\b|\b(?:compare|compared) fairly\b|\bfair comparison\b|\bframework[- ]on.{0,20}framework[- ]off\b', ('paired runs', '配对运行', 'paired evaluation', '配对对照')),
    ('dataset_split', r'数据集(?:切分|划分)|开发集.{0,18}测试集|按.{0,16}(?:事实|主题).{0,12}分组|\b(?:development|dev).{0,28}test(?:ing)?\b|\btest(?:ing)?.{0,28}(?:development|dev)\b|\bgroup(?:ed)? split\b', ('dataset split', '数据集切分', 'development test split', '分组留出')),
    ('paraphrase', r'改写|释义|同义表达|\bparaphras(?:e|es|ed|ing)\b', ('paraphrase', '改写', '同义表达')),
    ('synthetic_evaluation', r'合成(?:检索)?(?:基准|评测|测试)|\bsynthetic retrieval (?:benchmark|evaluation|test|success)\b', ('synthetic benchmark', '合成基准', 'synthetic evaluation')),
    ('measurement', r'测量|度量|取证|如何测|怎样测|怎么测|\bmeasure(?:s|d|ment|ments|ing)?\b|\bbe measured\b', ('measurement', '测量', '取证')),
    ('latency', r'延迟|耗时|首字时间|\blatency\b|\bTTFT\b|\bincremental (?:hook )?cost\b', ('latency', '延迟', '耗时', 'incremental cost')),
    ('reranking', r'重排|重排序|\brerank(?:er|ers|ing)?\b|\bcross-encoders?\b', ('reranker', '重排', 'cross-encoder')),
    ('document_frequency', r'文档频率|词频统计|\bdocument[- ]frequency\b|\bdf measurements?\b', ('document frequency', '文档频率', '词频统计')),
    ('hot_path', r'热路径|关键路径|\bhot path\b|\bcritical path\b', ('hot path', '热路径', 'critical path')),
    ('bounded_queue', r'有界队列|队列.{0,18}(?:上限|无界|无限增长)|\bbounded queues?\b|\bqueues?.{0,24}(?:without limit|unbounded)\b', ('bounded queue', '有界队列', 'queue limit')),
    ('preference', r'偏好|习惯|\bpreferences?\b', ('preference', '偏好', '习惯')),
    ('verification', r'验证|核验|确认|\bverif(?:y|ies|ied|ication)\b|\bvalidat(?:e|es|ed|ing|ion)\b|\bconfirm(?:s|ed|ation|ations)?\b', ('verify', '验证', 'confirmation', '确认')),
    ('automatic_admission', r'自动(?:采纳|接受|准入)|\bautomatic(?:ally)? accept(?:ance|ed|ing)?\b|\bauto[- ]accept(?:ance|ed|ing)?\b', ('automatic acceptance', '自动采纳', 'auto-accept')),
    ('retrieval_failure', r'检索(?:错误|失败|漏召回)|拒答错误|漏掉.{0,12}(?:记忆|证据)|\bretrieval (?:error|failure)s?\b|\babstention errors?\b|\bmissed relevant (?:memories|evidence)\b', ('retrieval failure', '检索错误', 'abstention error', '漏召回')),
    ('speculation', r'猜测|假设|未经验证|未验证|\bguesses?\b|\bassumptions?\b|\bunverified\b|\bspeculat(?:e|ion)\b', ('unverified', '未经验证', 'guess', '猜测')),
    ('transcript', r'转录|会话原文|完整(?:回合|对话)|\btranscripts?\b|\bcomplete turns?\b', ('transcript', '会话原文', '完整回合')),
    ('independent_evidence', r'独立(?:证据|确认|验证)|不同来源.{0,12}(?:证据|确认)|\bindependent (?:evidence|confirmation|validation)\b', ('independent evidence', '独立证据', 'independent confirmation')),
    ('deletion', r'永久删除|彻底删除|物理删除|\bpermanent(?:ly)? delet(?:e|ed|ion)\b|\bhard delet(?:e|ion)\b', ('permanent deletion', '永久删除', 'hard delete')),
    ('reuse_measurement', r'复用(?:机会|效果|链路|取证)|复用.{0,28}取证|召回机会.{0,40}上下文|\breuse (?:claim|measurement|evidence)\b|\brecall opportunity\b|\bcontext emission\b|\bdownstream task success\b', ('reuse measurement', '复用取证', 'context emission', '下游任务成功')),
    ('evidence_state', r'证据状态|证据.{0,16}(?:未知|不完整|覆盖)|看不到完整.{0,20}(?:怎么标|如何标)|\btranscript visibility\b|\bevidence (?:is |as )?unknown\b|\bmark evidence as unknown\b|\bclaiming full coverage\b', ('evidence state', '证据状态', 'evidence unknown', '证据未知')),
    ('audit', r'审计|事件表|追踪记录|\baudit (?:record|log|trail)s?\b|\bevent tables?\b', ('audit', '审计', 'event table', '事件表')),
    ('retention', r'保留策略|留存策略|保留期限|\bretention (?:policy|period)\b', ('retention', '保留策略', '留存策略')),
    ('bounded_retention', r'有界|上限|封顶|仍然增长|无限增长|\bbounded\b|\bcapped\b|\bcan still grow\b|\bgrow(?:s|ing)? without limit\b', ('bounded retention', '有界', 'capped', '封顶')),
)
_COMPILED = tuple((key, re.compile(pattern, re.I), aliases) for key, pattern, aliases in _CONCEPTS)

# One hit is normally too weak for a multi-term question.  These markers are
# exceptions because each pattern denotes a bounded engineering construct, not
# a broad topic.  A project-identity or document-frequency match can therefore
# carry recall across languages while ``database`` or ``HTTP`` still cannot.
DISTINCTIVE_CONCEPTS = frozenset({
    'project_scope', 'project_identity', 'fail_closed', 'hook_runtime',
    'compilation', 'positive_negative_test', 'paired_evaluation',
    'dataset_split', 'synthetic_evaluation', 'reranking',
    'document_frequency', 'bounded_queue', 'automatic_admission',
    'retrieval_failure', 'independent_evidence', 'deletion',
    'reuse_measurement', 'evidence_state', 'bounded_retention',
})


@lru_cache(maxsize=512)
def concepts(text: str) -> frozenset[str]:
    return frozenset(key for key, pattern, _ in _COMPILED if pattern.search(text[:5000]))


@lru_cache(maxsize=512)
def concept_tokens(text: str) -> tuple[str, ...]:
    """Return stable internal FTS markers for recognized product concepts."""
    return tuple(CONCEPT_TOKEN_PREFIX + key for key in sorted(concepts(text)))


def is_concept_token(value: str) -> bool:
    return value.startswith(CONCEPT_TOKEN_PREFIX)


def query_aliases(text: str, *, limit: int = 24) -> list[str]:
    matches = [aliases for _, pattern, aliases in _COMPILED if pattern.search(text[:500])]
    # Round-robin prevents a single concept from exhausting the rewrite budget.
    terms = [aliases[index] for index in range(4) for aliases in matches if index < len(aliases)]
    return list(dict.fromkeys(terms))[:limit]
