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
    ('conversation', r'对话|会话|\bconversations?\b', ('conversation', '对话', 'conversations', '会话')),
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
    ('archive', r'归档|\barchiv(?:e|es|ed)\b', ('archive', '归档', 'archived', '已归档')),
    ('recovery', r'恢复|复活|重新激活|\brestor(?:e|es|ed|ing|ation)\b|\breactivat(?:e|es|ed|ing|ion)\b', ('restore', '恢复', 'reactivate', '复活')),
    ('isolation', r'隔离|\bisolation\b|\bquarantin(?:e|es|ed|ing)\b', ('isolation', '隔离', 'quarantine', '已隔离')),
    ('proof', r'证明|证实|\bproof\b|\bprove[sd]?\b', ('proof', '证明', 'prove', '证实')),
    ('task', r'任务|\btasks?\b', ('task', '任务')),
    ('improvement', r'提升|改善|收益|\bimprov(?:e|es|ed|ing|ement)\b|\bgains?\b', ('improve', '提升', 'improvement', '改善')),
    ('supersession', r'替代|取代|\bsupersed(?:e|es|ed|ing)\b', ('supersede', '替代', 'superseded', '被替代')),
    ('history', r'历史|旧值|旧版本|\bhistory\b|\bhistorical\b|\bprevious\b', ('history', '历史', 'previous', '旧值')),
    ('budget', r'预算|\bbudgets?\b', ('budget', '预算')),
    ('context', r'上下文|\bcontext\b', ('context', '上下文')),
    ('version', r'版本|\bversions?\b', ('version', '版本')),
    ('threshold', r'阈值|\bthresholds?\b', ('threshold', '阈值')),
)
_COMPILED = tuple((key, re.compile(pattern, re.I), aliases) for key, pattern, aliases in _CONCEPTS)


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
