"""Conservative, local memory-use guards. No model call or corpus scan.

These guards recognize explicit instructions and self-declared obsolescence;
they are not a general semantic relevance classifier. Uncertain cases retain
the existing ranking. Decisions carry reasons for diagnostics and governance.
"""
from dataclasses import dataclass
from functools import lru_cache
import re
import hashlib
from agent_knowledge_bridge.knowledge_versions import blocked

# Bump whenever admission or retrieval boundaries change.  Existing reuse
# traces and LFHV shadow evidence then fail closed and are recomputed under the
# new policy instead of silently carrying forward an old decision.
POLICY_VERSION = 'memory-decisions-v7-budgeted-recovery'

def record_digest(row):
    return hashlib.sha256((str(row['title']) + '\x1f' + str(row['content'])).encode()).hexdigest()
_PIVOT = re.compile(r'(?:现在|接下来|本轮)(?:请)?只(?:解释|回答|讨论|处理|关注)|\b(?:now|instead)[, ]+(?:please )?(?:only |just )', re.I)
_EXCLUSION = re.compile(r'(?:与|跟|和)([^，。；\n]{2,60}?)(?:无关|不相关)|(?:不要|不用|无需|不需要|别)(?:再)?(?:引用|使用|注入|考虑|提及|套用|沿用|结合|按)([^，。；\n]{2,60})|\b(?:do not|don\x27t|without) (?:use |using |include |including |mention )([^.;\n]{2,80})', re.I)
_GENERIC = {'我的', '我们', '这个', '这些', '任何', '之前', '相关', '知识', '记录', '信息', '内容', '偏好', '项目', 'the', 'my', 'our', 'this', 'that', 'memory', 'knowledge', 'please', 'use', 'with', 'and', 'for'}

def terms(text):
    result = set()
    for word in re.findall(r'[a-z][a-z0-9_.-]+|[一-鿿]{2,}', text.casefold()):
        if re.fullmatch('[一-鿿]+', word):
            result.update(word[i:i+2] for i in range(len(word)-1))
        else:
            result.add(word)
    return result - _GENERIC

def instruction_text(query):
    # Quoted examples are data, not instructions to the retrieval policy.
    return re.sub(r'```[\s\S]*?```|“[^”]*”|"[^"\n]*"|`[^`]*`', ' ', query)

@dataclass(frozen=True)
class QueryIntent:
    focus: str
    excluded: tuple[str, ...]
    historical: bool = False
    requested_subjects: frozenset = frozenset()
    memory_disabled: bool = False
    general_explanation: bool = False
    excluded_projects: tuple[str, ...] = ()
    # These flags represent an explicit user boundary.  They are kept
    # separate from ``excluded`` because a phrase such as "another project"
    # does not name a token that can be compared with a record title.
    exclude_project_scope: bool = False
    exclude_user_preferences: bool = False

def query_intent(query, project_key=None):
    instructions = instruction_text(query)
    pivots = list(_PIVOT.finditer(instructions))
    # Locate in the unmodified query only when offsets remain unambiguous.
    focus = instructions[pivots[-1].start():] if pivots else query
    matches = [m for m in _EXCLUSION.finditer(instructions)
               if not re.search(r'(?:不是|并非|不能说|并不是)\s*$', instructions[max(0,m.start()-8):m.start()])]
    excluded = tuple(next(g for g in m.groups() if g) for m in matches)
    for match in matches:
        focus = focus.replace(match.group(0), ' ')
    switch = re.search(r'(?:不用|无需|不要)重复[。；，\s]+((?:请|帮我)(?:解释|介绍|回答)[\s\S]+)', instruction_text(focus))
    if switch:
        focus = switch.group(1)
    historical = bool(re.search(r'查看历史|追溯|回顾|历史(?:决定|决策|配置|规则)|(?:以前|原来|过去).{0,12}(?:多少|什么)|\b(?:historical|previous) (?:decision|policy|setting)|\bwhy .{0,40}(?:revoked|replaced)', instructions, re.I))
    requested = terms(_EXCLUSION.sub(' ', instruction_text(focus)))
    disabled = bool(re.search(r'(?:不要|不用|无需)(?:再)?(?:调用|引用|使用|注入)(?:任何|全部|所有)?(?:历史|共享|个人|长期)?(?:记忆|知识库)|\b(?:do not|don\x27t) use (?:any |shared |personal )?memor(?:y|ies)\b', instructions, re.I))
    explanation = bool(re.search(r'^(?:请)?(?:解释|介绍|为什么|科普)|原理|起源|区别|差异|一般来说|一般.{0,12}(?:原则|规律|做法)|是什么意思|什么含义|\b(?:why|what is|what are|explain)\b|\b(?:history|origin|acronym|biography|logo|itinerary|chords)\b|天气|百科|缩写|全称|传记|行程|和弦', focus, re.I))
    explanation = explanation or bool(re.search(r'如果.{0,30}(?:是不是|是否|会不会)', focus))
    personal = bool(re.search(r'我的|我们|偏好|默认|按.{0,8}习惯|\b(?:my|our|preference|default)\b', focus, re.I))
    # Explicit scope exclusions must survive FTS and expansion.  Without an
    # explicit flag, a query can mention a project name only to say that its
    # policy must not be reused, while the same subject still gets returned.
    exclude_project_scope = bool(re.search(
        r'另一个项目|新项目|(?:不是|并非).{0,20}项目.{0,12}(?:规则|配置|设置|策略)|'
        r'(?:不要|不用|无需|不需要|别).{0,24}(?:项目)?(?:配置|设置|策略|规则).{0,16}(?:套|拿|结合|引用)|'
        r'只问.{0,20}(?:词义|区别|含义).{0,20}(?:不要|不用|无需|别).{0,12}(?:项目|配置|规则)|'
        r'一般.{0,16}(?:产品|设计).{0,12}(?:原则|规律)',
        instructions, re.I))
    exclude_user_preferences = bool(re.search(
        r'(?:不用|不要|无需|不需要|别).{0,24}(?:我的|个人)?(?:常用|默认|个人)?(?:风格|偏好|习惯|设置|主题|页面颜色|视觉)|'
        r'(?:不是|并非).{0,20}(?:我的|个人).{0,12}(?:偏好|设置|习惯)|'
        r'(?:别|不要).{0,12}(?:按|沿用).{0,24}(?:之前|以前|上次).{0,16}(?:页面|网页|界面)?(?:颜色|风格|主题|方案)|'
        r'一般.{0,16}(?:产品|设计).{0,12}(?:原则|规律)',
        instructions, re.I))
    projects = list(re.findall(r'不是([^，。；\n]{1,30}?)项目的(?:配置|设置|策略)问题', instructions))
    if project_key and re.search(r'另一个项目|新项目|别拿[^，。；\n]{0,20}项目.{0,12}(?:套|拿)|(?:不要|不用|无需|不需要|别).{0,20}(?:项目)?(?:配置|设置|策略)', instructions):
        projects.append(project_key)
    projects = tuple(dict.fromkeys(projects))
    return QueryIntent(focus.strip(), excluded, historical, frozenset(requested), disabled,
                       explanation and not personal, projects, exclude_project_scope,
                       exclude_user_preferences)

@lru_cache(maxsize=512)
def _obsolete(title, content):
    # A current rule may mention an obsolete predecessor. Require either an
    # explicitly old title or a declaration about THIS record, never a keyword
    # anywhere in a document describing deprecation.
    content = instruction_text(content)
    declaration = re.search(r'(?:此|本|该)(?:条|项|份)?(?:旧)?(?:规则|决定|决策|记录|知识|值|配置|政策)[^。；\n]{0,30}(?:已废止|已失效|已作废|已被.{0,20}(?:替代|取代))|\bthis (?:rule|decision|record|policy) (?:is|has been) (?:obsolete|revoked|superseded|replaced)', content, re.I)
    if declaration and re.search(r'如果|假如|若|不要认为|不能认为|并非|并不是|\bif\b', content[max(0,declaration.start()-16):declaration.end()], re.I):
        declaration = None
    if declaration:
        return True
    old_title = re.search(r'旧|已废止|已失效|\b(?:old|obsolete|superseded|retired)\b', title, re.I)
    revoked = re.search(r'已[^。；\n]{0,24}(?:废止|失效|作废)|已被[^。；\n]{0,30}(?:替代|取代)|\b(?:was|is|has been) (?:superseded|revoked|replaced)\b', content, re.I)
    return bool(old_title and revoked)

def obsolete(row):
    return _obsolete(str(row['title'])[:256], str(row['content'])[:4000])

def rejection_reason(row, intent):
    version_reason = blocked(row, historical=intent.historical)
    if version_reason:
        return version_reason
    if intent.historical and 'status' in row.keys() and row['status']=='archived' and not ('superseded_by' in row.keys() and row['superseded_by']):
        return 'ordinary_archive_not_version_history'
    if intent.memory_disabled:
        return 'memory_opt_out'
    if intent.exclude_project_scope and 'scope' in row.keys() and row['scope'] == 'project':
        return 'explicitly_excluded_project_scope'
    if intent.exclude_user_preferences and 'knowledge_type' in row.keys() and row['knowledge_type'] == 'preference':
        return 'explicitly_excluded_user_preference'
    if obsolete(row) and not intent.historical:
        return 'explicitly_obsolete'
    if 'knowledge_type' in row.keys() and row['knowledge_type'] == 'preference' and intent.general_explanation:
        return 'general_question_not_preference_use'
    # Compare exclusions with the subject, not all body words. A long technical
    # procedure mentioning an excluded topic is not necessarily ABOUT it.
    subject = terms(row['title'])
    if 'scope' in row.keys() and row['scope'] == 'project' and any(subject & terms(p) for p in intent.excluded_projects):
        return 'explicitly_excluded_project'
    for excluded in intent.excluded:
        excluded_overlap = subject & terms(excluded)
        # A shared project name alone must not veto a different, explicitly
        # requested subject in the same project.
        if excluded_overlap and len(subject & intent.requested_subjects) <= len(excluded_overlap):
            return 'explicitly_excluded'
    return None

def filter_candidates(candidates, fallback, intent):
    accepted, retained, omitted = [], [], []
    for hit in candidates:
        reason = rejection_reason(hit.row, intent)
        if reason:
            omitted.append({'knowledge_id': hit.id, 'reason': reason})
        else:
            accepted.append(hit)
    for row in fallback:
        reason = rejection_reason(row, intent)
        if reason:
            omitted.append({'knowledge_id': row['id'], 'reason': reason})
        else:
            retained.append(row)
    return tuple(accepted), retained, omitted

_NO_SAVE = re.compile(r'(?:不要|不用|无需|别)(?:再)?(?:记住|保存|记录)|不要当成.{0,12}(?:长期|偏好|决定)|临时.{0,12}(?:不用|不代表)|\b(?:do not|don\x27t) (?:remember|save)', re.I)
# Quoted or attributed statements are not accepted automatically, but remain
# reviewable evidence.  These stronger phrases mean the user explicitly
# withholds a durable decision and therefore fail closed at admission.
_REJECT_NO_SAVE = re.compile(r'供讨论|还没(?:有)?决定|尚未决定|只是(?:问|讨论|假设)|not decided', re.I)
_REQUEST = re.compile(r'请记住|请保存|记住这个|记住：|记一下|记着|记住|保存|存一下|\b(?:please )?remember\b', re.I)
_DURABLE = re.compile(r'今后|以后|往后|接下来|长期|默认|正式|决定|决策|从现在起|这套(?:习惯|规则|偏好)|\b(?:always|default|from now on|decision|policy)\b', re.I)
_USER_SCOPE = re.compile(r'跨项目|各个项目|所有项目|我的(?:长期|个人).{0,12}(?:偏好|习惯)|\ball (?:my )?projects\b', re.I)
_SPECULATION = re.compile(r'没有.{0,12}(?:证据|验证|日志)|只是猜测|未经验证|\b(?:no evidence|unverified|just a guess|only a guess)\b', re.I)
_SENSITIVE_STORAGE = re.compile(r'(?:api[ _-]?key|密钥|密码|口令|password|token|credential|secret)', re.I)

def uncertain_only(turn):
    return not turn.tools and bool(_SPECULATION.search(turn.assistant_text)) and not bool(_REQUEST.search(turn.user_text))

def grounded_user_proposal(proposal, turn):
    """Return exact evidence and eligibility, not model-generated paraphrases.

    Auto acceptance is deliberately narrow: the entire user statement must be
    quoted, explicitly persistent, non-interrogative, and a preference/decision.
    Partial quotes remain candidates. Legacy integrations without quotes also
    remain candidates; the built-in reviewer requires quotes for ordinary items.
    """
    quotes = proposal.get('source_quotes')
    if quotes is None:
        return None
    if not isinstance(quotes, list) or not 1 <= len(quotes) <= 3:
        raise ValueError('Invalid source quotes')
    role = proposal.get('source_role', 'user')
    if role not in {'user', 'assistant'}:
        raise ValueError('Invalid source role')
    source = turn.user_text if role == 'user' else turn.assistant_text
    if role == 'assistant' and (not turn.tools or _SPECULATION.search(source)):
        raise ValueError('Assistant-only assertion lacks observed work')
    exact = []
    for quote in quotes:
        if not isinstance(quote, str) or len(quote.strip()) < 8 or quote not in source:
            raise ValueError('Ordinary memory must quote the user verbatim')
        exact.append(quote.strip())
    if _NO_SAVE.search(turn.user_text) or _REJECT_NO_SAVE.search(turn.user_text):
        raise ValueError('User did not authorize a durable assertion')
    instructions = instruction_text(turn.user_text)
    # Keep the admission predicate self-contained.  The adapter also has a
    # fast path for secret-only turns, but callers that use this contract
    # directly must receive the same fail-closed result.
    if _SENSITIVE_STORAGE.search(instructions):
        raise ValueError('Sensitive credentials cannot become reusable knowledge')
    explicit = (role == 'user' and len(exact) == 1 and exact[0] == turn.user_text.strip() and len(exact[0]) <= 1000
                and bool(_REQUEST.search(instructions)) and bool(_DURABLE.search(instructions))
                and not re.search(r'示例|转述|引用|这句话|假设|猜测|如果|仅当|除非|当.{0,16}时|\b(?:example|quote|hypothesis|guess|if|unless)\b', instructions, re.I)
                and not re.search(r'(?:api[ _-]?key|密钥|密码|口令|password|token|credential|secret)', instructions, re.I)
                and not re.search(r'[?？]|是否|要不要|\b(?:should|could|would)\b', turn.user_text, re.I)
                and proposal.get('knowledge_type') in {'preference', 'decision'})
    scope = 'user' if role == 'user' and _USER_SCOPE.search(turn.user_text) else 'project'
    return {'content': '\n'.join(exact), 'scope': scope, 'auto_accept': explicit}
