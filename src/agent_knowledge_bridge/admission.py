"""Topic-independent retention gates, evaluated before candidate publication.

The reviewer can flag temporary/uncertain evidence but cannot grant activation.
Only exact user directives or the separate typed recovery validator can do so.
These local guards recognize explicit language, not arbitrary semantic truth.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import re

from .claude_transcript import redact_text


class AdmissionRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Match an instruction to the agent, never narrative "I remember ...".
_REQUEST = re.compile(
    r'^(?:请)?(?:帮我|麻烦|务必|一定要|你要|你)?'
    r'(?:(?:长期|持续)?(?:记住|保存|记录|保留|跟踪|追踪)|持续关注)(?!了)|'
    r'^(?:please\s+)?(?:always\s+)?(?:remember|save|retain|record|track|monitor)\b', re.I)
_LONG_TERM = re.compile(
    r'长期|持续|今后|以后|往后|默认|从现在起|后续统一|'
    r'\b(?:long[- ]term|ongoing|persistently|from now on|always|default)\b', re.I)
_RETENTION_HORIZON = re.compile(
    r'长期|持续|今后|以后|往后|从现在起|'
    r'\b(?:long[- ]term|ongoing|persistently|from now on|always)\b', re.I)
_UNCERTAIN_FRAGMENT = (
    r'(?:(?:this|that|it)\s+(?:is\s+)?(?:only\s+|just\s+)?'
    r'(?:a\s+guess|unverified|not\s+(?:confirmed|verified))|'
    r'(?:this|that|it)\s+has\s+not\s+been\s+(?:verified|confirmed)(?:\s+yet)?|'
    r'I(?:\s+am|[\x27’]m)\s+not\s+sure'
    r'(?:\s+(?:if|whether)\s+(?:that|this|it)(?:\s+is|[\x27’]s)\s+(?:true|correct|accurate))?|'
    r'maybe|perhaps|probably|'
    r'这(?:只是|仅是|是)?(?:猜测|假设|未确认)|只是猜测|尚未确认|我(?:还)?不确定)')
_TEMPORARY_FRAGMENT = (
    r'(?:(?:(?:this|that|it)\s+(?:is\s+)?(?:only\s+|just\s+)?)?'
    r'(?:temporary|(?:only\s+)?(?:for now|for today))|'
    r'这(?:只是|仅是|是)?(?:暂定|临时的)|仅限今天|仅本次)')
_QUALIFIER_FRAGMENT = re.compile(
    rf'^(?:(?:but|and)\s+|但是|但|而且)?(?:(?P<uncertain>{_UNCERTAIN_FRAGMENT})|(?P<temporary>{_TEMPORARY_FRAGMENT}))'
    r'[。.!！\s]*$', re.I)
_UNCERTAIN = re.compile(
    r'我(?:猜|怀疑|(?:还)?不确定)|也许|大概是|可能(?:是|会|有|需要|导致|因为|采用|使用)|'
    r'只是猜测|尚未确认|还未确认|未经验证|待验证|未验证的|还需要讨论|'
    r'还没(?:有)?决定|尚未决定|供讨论|只是(?:问|讨论|假设)|'
    r'\b(?:maybe|perhaps|might|probably|hypothesis|unverified|not confirmed|not verified|just a guess|not sure|'
    r'has not been (?:verified|confirmed))\b', re.I)
_TEMPORARY = re.compile(
    r'暂时|暂定|仅本次|只在本次|本轮先|这次先|临时(?:使用|改为|决定|方案|状态)|'
    r'(?:本轮|本次|这次|这轮)(?:先|只|仅|默认|使用|采用|改为|不用|不要|执行)|'
    r'(?:^|[，,。；;\n])\s*(?:我|我们)?(?:今天|今晚|明天|本周|这周|下周|最近)(?!起|开始)|'
    r'(?:目前|现在)(?:我|我们)?正在|'
    r'\b(?:temporarily|for now|this time only|just for today|currently|today|tomorrow|this week)\b', re.I)
_QUESTION = re.compile(r'[?？]|是否|要不要|(?:吗|么|如何|怎么样)[。！]?\s*$|\b(?:should|could|would)\b', re.I)
_INTERROGATIVE = re.compile(
    r'[?？]|(?:吗|么|如何|怎么样)[。！]?\s*$|^\s*(?:你觉得|请问|要不要|是否)|'
    r'^\s*(?:should|could|would)\b', re.I)
_ATTRIBUTED = re.compile(
    r'示例|转述|引用|这句话|别人(?:说|建议)|有人(?:说|建议)|听说|据说|假设|如果|仅当|除非|当.{0,16}时|'
    r'\b(?:example|quote|hypothesis|guess|if|unless)\b', re.I)
_DIRECTIVE = re.compile(
    r'^(?:请)?(?:(?:今后|以后|往后|从现在起|默认)(?:(?:所有|各个)项目(?:的)?)?|'
    r'(?:本仓库|本项目|这个项目)(?:今后|以后|后续)(?:统一|默认))'
    r'(?:请|你|都|一律|统一|默认|先|再|回复|每周[一二三四五六日天]|每天|每次)*'
    r'(?:使用|采用|保持|遵循|输出|回答|解释|执行|验证|检查|核对)|'
    r'^(?:please\s+)?(?:for all (?:my |future )?projects[,\s]+)?'
    r'(?:always|from now on|by default)\b[,\s]+'
    r'(?:please\s+)?(?:use|write|answer|respond|explain|run|keep|check|verify)\b', re.I)
_DENY_STORAGE = re.compile(
    r'(?:不要|不用|无需|别)(?:再|长期|自动|持续)*(?:记住|保存|记录|保留|跟踪|追踪)|'
    r'\b(?:do not|don\x27t|never)\s+(?:(?:automatically|persistently)\s+)?'
    r'(?:remember|save|retain|record|track|monitor)\b', re.I)
_QUOTED = re.compile(
    r'```[\s\S]*?```|~~~[\s\S]*?~~~|“[^”]*”|「[^」]*」|'
    r'"[^"\n]*"|(?<!\w)\x27[^\x27\n]+\x27(?!\w)|`[^`]*`')
_CONTINUATION_DIRECTIVE = re.compile(
    r'^(?:(?:please|then)\s+)?(?:use|write|answer|respond|explain|run|keep|check|verify)\b|'
    r'^(?:请|再|并|继续)*(?:使用|采用|保持|遵循|输出|回答|解释|执行|验证|检查|核对)|'
    r'^(?:不要|不得|不能|别)(?:把|将|使用|采用|引入)|'
    r'^不(?:使用|采用|引入)|'
    r'^(?:代码(?:里|中)的标识符|错误信息)(?:保留|保持)', re.I)


def task_only_assertion(text: str) -> bool:
    """A bounded task command is not a durable fact merely because it is quoted."""
    return bool(re.match(
        r'^(?:请|帮我|麻烦)*(?:检查|排查|修复|运行|执行|写一份|生成一份|处理一下)|'
        r'^(?:please\s+)?(?:check|inspect|debug|fix|run|write|generate)\b', text, re.I)
        and not stable_user_directive(text))


def canonical_quote(text: str) -> str:
    # Remove only a neutral confirmation wrapper, never a qualifier, negation,
    # version, platform, attribution or arbitrary semantically similar wording.
    return re.sub(r'^(?:补充确认|再次确认|to confirm)\s*[:：]\s*', '', text, flags=re.I)

# Credential *values/storage requests* are sensitive, technical nouns are not.
# This is shared by the pre-review fast path and the per-evidence Core gate.
_CREDENTIAL_NOUN = (
    r'(?:\b(?:api[ _-]?keys?|(?:access[ _-]?)?tokens?|passwords?|credentials?|secrets?)\b|密钥|密码|口令)')
_CREDENTIAL_CONCEPT = re.compile(
    r'\btokens?\s*(?:budgets?|limits?|counts?|usage|consumption|costs?|lengths?|windows?|'
    r'预算|上限|数量|用量|消耗|开销|长度)|'
    r'\bpassword\s+(?:hash(?:ing)?|policy|rotation|manager|validation)\b|'
    r'\bsecret\s+(?:manager|management|scanning)\b|'
    r'\bapi[ _-]?keys?\s+(?:rotation|management|validation|policy)\b|'
    r'(?:密码|密钥)(?:哈希|策略|轮换|管理|校验)', re.I)
_CREDENTIAL_VALUE = re.compile(
    _CREDENTIAL_NOUN + r'\s*(?:[:=：]|(?:is|was)\s+|是|为)\s*\S+', re.I)
_CREDENTIAL_STORAGE = re.compile(
    r'(?:记住|保存|存储|记录|写入|remember\b|save\b|store\b|record\b|retain\b)'
    r'[^。.!！?？;；\n]{0,50}' + _CREDENTIAL_NOUN, re.I)
# Compatibility export for older adapter imports; decisions use the full
# evidence predicate below, including concept/value distinctions.
SECRET_STORAGE_REQUEST = _CREDENTIAL_STORAGE


def sensitive_evidence_reason(text: str) -> str | None:
    """Explain blocked values/requests without blocking words such as tokenizer."""
    concepts_removed = _CREDENTIAL_CONCEPT.sub(' technical concept ', text)
    if _CREDENTIAL_STORAGE.search(concepts_removed):
        return 'sensitive_storage_request'
    if ('[REDACTED]' in text or redact_text(text, len(text) + 1) != text
            or _CREDENTIAL_VALUE.search(concepts_removed)):
        return 'sensitive_content'
    return None


def sensitive_storage_only(text: str) -> bool:
    # Mixed turns still reach the reviewer (with existing redaction). Only the
    # sensitive proposals are rejected; independent assertions remain usable.
    bounds = _boundaries(text)
    clauses = [text[a:b].strip() for a, b in zip(bounds, bounds[1:]) if text[a:b].strip()]
    return bool(clauses) and all(sensitive_evidence_reason(c) == 'sensitive_storage_request'
                                 for c in clauses)


def _instructions(text: str) -> str:
    # Retention requests in examples/fences are not the user's authorization.
    return _QUOTED.sub(' ', text)


def has_quoted_evidence(text: str) -> bool:
    """Quoted data (including unfinished quotes) cannot grant authorization."""
    return bool(_QUOTED.search(text) or re.search(
        r'```|~~~|[“”「」"`]|(?<!\w)\x27(?=\S)', text))


def storage_denied(text: str) -> bool:
    return bool(_DENY_STORAGE.search(_instructions(text)))


def _boundaries(text: str) -> list[int]:
    """Unambiguous sentence ends, not decimal/path dots or common abbreviations."""
    result = [0]
    for match in re.finditer(r'[。！？!?；;\n]|\.(?=\s|$)', text):
        if match.group() == '.':
            token = re.search(r'([A-Za-z.]+)$', text[:match.start()])
            word = token.group(1).lower() if token else ''
            if (word in {'e.g', 'i.e', 'mr', 'mrs', 'ms', 'dr', 'prof', 'vs', 'etc'}
                    or re.fullmatch(r'[a-z](?:\.[a-z])*', word)):
                continue
        result.append(match.end())
    return result + [len(text)]


_CLAUSE_BOUNDARY = re.compile(
    r'[,，]\s*(?=(?:(?:but|and)\s+)?(?:'
    r'(?:can|could|would|will|do|does|did|have|has|had|are|were)\s+you\b|'
    r'(?:how|what|why|where|which)\s+(?:do|does|did|can|could|would|should|will)\b|'
    r'(?:how|what|why|where)\s+(?:is|are|was|were)\b|'
    r'I(?:\s+am|[\x27’]m)\s+not\s+sure\b|'
    r'(?:this|that|it)\s+is\s+(?:only\s+)?(?:temporary|unverified)\b)|'
    r'(?:你能|你觉得|请问|怎么|如何|是否|这只是|我还不确定|但是我不确定))', re.I)
_NON_ASSERTION = re.compile(
    r'^(?:please|can|could|would|should|will|do|does|is|are|was|were|has|have|how|what|why|where|which|when|who|whether|'
    r'help|tell|explain|show|give|write|use|run|check|verify|remember|save|thanks|thank)\b|'
    r'^(?:I|we)\s+(?:want|need|plan|hope|intend)\s+to\b|'
    r'^(?:I|we)(?:\s+am|[\x27’]m|\s+are|[\x27’]re)\s+(?:thinking|planning)\b|'
    r'^(?:请|帮我|麻烦|你能|你觉得|怎么|如何|是否|谢谢|我(?:们)?(?:打算|计划|想要|希望))', re.I)
_DECLARATIVE = re.compile(
    r'^(?:I|we)\s+(?:(?:do not|don[\x27’]t|never)\s+)?(?:am|are|was|were|have|had|use|prefer|work|live|own|need|'
    r'keep|run|attend|bought|built|caught|went|saw|got|[a-z]+ed)\b|'
    r'^(?:I[\x27’](?:m|ve)|we[\x27’](?:re|ve))\s+\w|'
    r'^(?:my|our|the|this|that|these|those|[A-Z][\w.-]*)\b[^.!?。！？\n]{0,80}?'
    r'\b(?:is|are|was|were|has|have|had|does\s+not\s+use|uses|runs|supports|requires|contains|[a-z]+ed)\b|'
    r'^[\w一-鿿][^，,。！？!?\n]{0,80}?(?:是|有|使用|采用|支持|位于|需要|包含|负责|完成)', re.I)

# Classification strips discourse markers only; persisted text stays verbatim.
# A request/plan is not made durable by a past tense, contraction or "also".
_DISCOURSE = re.compile(
    r'^(?:(?:also|and|but|oh|well|actually|anyway|so)[,\s]+|'
    r'by the way,?\s+|speaking of [^,。!?\n]{1,40},\s+|(?:另外|但是|而且)[，,]?\s*)+', re.I)
_INTENT_OR_REQUEST = re.compile(
    r'^(?:I|we)(?:\s+(?:am|are|was|were|have been|had been)|'
    r'[\x27’](?:m|re)|[\x27’]ve\s+been)\s+'
    r'(?:(?:also|still|just|really|actually|currently)\s+)*'
    r'(?:thinking|planning|considering|looking|trying|hoping|wondering|leaning|'
    r'interested|curious|concerned|worried)\b|'
    r'^(?:I|we)\s+(?:think|suppose|guess|wonder|(?:want|plan|hope|intend)\s+to)\b|'
    r'^我(?:们)?(?:正在考虑|正考虑|打算|计划|想要|希望)', re.I)
_DEPENDENT_ASSERTION = re.compile(
    r'^(?:it|this|that|these|those)(?:[\x27’](?:s|re|ve|d)\s|'
    r'\s+(?:is|are|was|were|has|have|had|seems|seemed|helped|made|worked)\b)', re.I)
_COORDINATOR = re.compile(r'[,，]|\b(?:and|but|whereas)\b|但是|而且', re.I)
_DEPENDENT_HEAD = re.compile(
    r'^(?:which|where|whose|that|because|since|while|when|although|unless|if|as)\b|'
    r'^(?:因为|由于|如果|假如|除非|仅当|当.+时)', re.I)
# A verbatim substring can still be misleading if a surrounding negation or
# limit was clipped. Compare these explicit markers, not inferred semantics.
_RESTRICTION_MARKER = re.compile(
    r'\b(?:not|never|no|without|except)\b|\bonly\s+(?:in|for|when|on|to|under|at|with)\b|'
    r'不|没有|未|仅(?:在|对)|只(?:在|对|能|适用|用于)', re.I)
_EXPLICIT_SUBJECT = re.compile(
    r'^(?:(?:I|we|he|she|they)\b|(?:I|we|he|she|they)[\x27’](?:m|re|ve|s|d)\s)|'
    r'^(?:(?:my|our|the|this|that|these|those)\s+[^,，.!?。！？;；\n]{1,60}?|'
    r'(?-i:[A-Z][\w.-]*(?:\s+[A-Z][\w.-]*){0,3}))\s+'
    r'(?:is|are|was|were|has|have|had|does\s+not\s+use|uses|runs|supports|requires|contains)\b|'
    r'^(?:我|我们|我的|我们的|本项目|该服务|这个项目|当前项目)[^，,。！？!?;；\n]{0,60}'
    r'(?:是|有|使用|采用|支持|位于|需要|包含|负责|完成)', re.I)
_EPISTEMIC_HEAD = re.compile(r'^(?:I|we)\s+(?:think|suppose|guess|wonder)\b', re.I)


def _clause_head(text: str) -> bool:
    """Require an explicit statement subject; not a relative/qualifier fragment."""
    text = _DISCOURSE.sub('', text.strip())
    return bool(text and not _DEPENDENT_HEAD.search(text)
                and not _DEPENDENT_ASSERTION.search(text)
                and not _EPISTEMIC_HEAD.search(text)
                and not re.match(rf'(?:{_UNCERTAIN_FRAGMENT}|{_TEMPORARY_FRAGMENT})', text, re.I)
                and (_EXPLICIT_SUBJECT.search(text) or _INTENT_OR_REQUEST.search(text))
                and (_DECLARATIVE.search(text) or _INTENT_OR_REQUEST.search(text)))


def _independent_clause_cuts(source: str, bounds: list[int]) -> list[int]:
    """Structural coordination only, never extract presuppositions from a plan.

    Both sides need their own subject/predicate. A qualifier, attribution,
    conditional, parenthesis or relative clause cannot manufacture independence.
    The reviewer still chooses the exact assertion; these cuts grant no truth,
    scope or activation authority.
    """
    cuts = []
    for a, b in zip(bounds, bounds[1:]):
        sentence = source[a:b]
        if has_quoted_evidence(sentence):
            continue
        depth, previous = 0, 0
        for match in _COORDINATOR.finditer(sentence):
            for char in sentence[previous:match.start()]:
                if char in '([{（【':
                    depth += 1
                elif char in ')]}）】':
                    depth = max(0, depth - 1)
            previous = match.end()
            if depth:
                continue
            left, right = sentence[:match.start()], sentence[match.end():]
            if (not _clause_head(left) or not _clause_head(right)
                    or _UNCERTAIN.search(left) or _TEMPORARY.search(left)
                    or _ATTRIBUTED.search(left)
                    or _EPISTEMIC_HEAD.search(_DISCOURSE.sub('', left.strip()))):
                continue
            # Keep the separator as its own (non-factual) span. Neither stored
            # assertion acquires a dangling conjunction or loses punctuation.
            cuts.extend((a + match.start(), a + match.end()))
    return cuts


def evidence_boundaries(text: str) -> list[int]:
    """Sentence ends plus explicit question/qualifier clauses, never bare commas.

    A leading "Today," or "Maybe," remains attached to its assertion. Quoted
    data cannot create a new assertion boundary inside the quoted region.
    """
    quoted = [(match.start(), match.end()) for match in _QUOTED.finditer(text)]
    sentences = _boundaries(text)
    cuts = sentences + _independent_clause_cuts(text, sentences) + [
        match.end() for match in _CLAUSE_BOUNDARY.finditer(text)]
    return sorted({cut for cut in cuts if not any(a < cut < b for a, b in quoted)})


def independent_assertions(proposal: dict, source: str, quotes: list[str], *,
                           evidence_index=None) -> list[str]:
    """Retain bounded verbatim assertions from a rejected *mixed* proposal.

    This is a conservative fallback, not a semantic extractor or authorization.
    It cannot override model uncertainty, explicit retention exceptions, quoted
    data, source qualifiers, or a storage prohibition. Only supplied quotes may
    contribute text; no unrelated assertion is borrowed from the source turn.
    """
    if proposal.get('retention', 'durable') != 'durable':
        return []
    evidence_index = evidence_index or EvidenceContext(source)
    selected = []
    for quote in sorted(quotes, key=source.find):
        if has_quoted_evidence(quote):
            continue
        bounds = evidence_index.bounds if quote == source else evidence_boundaries(quote)
        for a, b in zip(bounds, bounds[1:]):
            text = quote[a:b].strip().rstrip(',，').strip()
            # Bound the retained material rather than preserving a raw turn.
            if not 8 <= len(text) <= 600 or text in selected:
                continue
            assertion = _DISCOURSE.sub('', text)
            if (_NON_ASSERTION.search(assertion) or _INTENT_OR_REQUEST.search(assertion)
                    or _DEPENDENT_ASSERTION.search(assertion) or not _DECLARATIVE.search(assertion)
                    or has_quoted_evidence(text)):
                continue
            context = evidence_index.context([text])
            if sensitive_evidence_reason(context) or evidence_index.clipped_restriction([text]):
                continue
            try:
                category, _ = retention(proposal, context, role='user', request_evidence=text)
            except AdmissionRejected:
                continue
            if category == 'durable':
                selected.append(text)
            if len(selected) == 3:
                return selected
    return selected


def assertion_clauses(text: str) -> list[str]:
    """Conservative units for authorization/scope, not a semantic extractor."""
    text = re.sub(r'\b(from now on|by default),\s*', r'\1 ', text, flags=re.I)
    text = re.sub(r'\b(for all (?:(?:my|future) )?projects),\s*', r'\1 ', text, flags=re.I)
    bounds = _boundaries(text)
    return [part.strip() for a, b in zip(bounds, bounds[1:])
            for part in re.split(r'[,，]|\b(?:and|but|while|whereas)\b|并且|但是|而且', text[a:b], flags=re.I)
            if part.strip()]


def _activation_clauses(text: str) -> list[str]:
    # Never remove quoted material to approve the remaining instruction while
    # persisting the *original* unapproved material. Keep such records reviewable.
    if has_quoted_evidence(text):
        return []
    return assertion_clauses(text)


def explicit_retention_request(text: str, *, for_activation: bool = False) -> bool:
    """The same instruction clause must request storage and a durable horizon."""
    instructions = _instructions(text).strip()
    if (re.search(r'[?？]', instructions)
            or re.search(r'示例|转述|引用|别人说|\b(?:example|quote)\b', instructions, re.I)
            or (for_activation and _ATTRIBUTED.search(instructions))):
        return False
    bounds = _boundaries(instructions)
    clauses = (_activation_clauses(text) if for_activation else
               [instructions[start:end].strip() for start, end in zip(bounds, bounds[1:])
                if instructions[start:end].strip()])
    matches = [bool(_REQUEST.search(clause) and (
        _RETENTION_HORIZON.search(clause) or (for_activation and re.search(
            r'正式|决策|默认|\b(?:decision|policy|default)\b', clause, re.I))))
        for clause in clauses]
    # Do not activate an unrelated assertion bundled with a storage request.
    return bool(matches) and (all(matches) if for_activation else any(matches))


def retention(proposal: dict, evidence: str, *, role: str,
              request_evidence: str | None = None) -> tuple[str, bool]:
    """Return retention class and an evidence-bound explicit storage request.

    A request must occur in the quoted evidence, not an unrelated sentence of
    the turn. Unknown model metadata fails closed instead of implying durability.
    """
    declared = proposal.get('retention', 'durable')
    if declared not in ('durable', 'temporary', 'uncertain'):
        raise AdmissionRejected('invalid_retention')
    instructions = _instructions(evidence)
    if storage_denied(evidence):
        raise AdmissionRejected('memory_storage_opt_out')
    authorization = _instructions(request_evidence) if request_evidence is not None else instructions
    requested = role == 'user' and explicit_retention_request(authorization)
    # A prohibition on treating guesses as facts is a rule, not a guess.
    assertions = re.sub(r'(?:不要|不得|不能|别)(?:把|将)[^。；\n]{0,80}(?:当作|作为)[^。；\n]*', '', instructions)
    # Context collection and classification share the same qualifier grammar;
    # retaining "This is temporary" must actually affect the admission result.
    qualifiers = {match.lastgroup for clause in assertion_clauses(instructions)
                  if (match := _QUALIFIER_FRAGMENT.fullmatch(clause))}
    category = ('uncertain' if declared == 'uncertain' or _UNCERTAIN.search(assertions)
                or _INTERROGATIVE.search(instructions) or 'uncertain' in qualifiers
                else 'temporary' if declared == 'temporary' or _TEMPORARY.search(instructions)
                or 'temporary' in qualifiers
                else 'durable')
    if category != 'durable' and not requested:
        raise AdmissionRejected(f'{category}_knowledge')
    return category, requested


def stable_user_directive(text: str) -> bool:
    """A deterministic, unconditional user instruction, not a generic fact."""
    if (has_quoted_evidence(text) or not _LONG_TERM.search(text)
            or _QUESTION.search(text) or _ATTRIBUTED.search(text)):
        return False
    bounds = _boundaries(text)
    sentences = [text[a:b].strip() for a, b in zip(bounds, bounds[1:]) if text[a:b].strip()]
    # A comma/"and" can continue an instruction within the same sentence, but
    # a new sentence needs its own durable directive. "Always use ... . Use ...
    # for this task." must not grant permanent approval to the latter action.
    for sentence in sentences:
        clauses = assertion_clauses(sentence)
        if (not clauses or not _DIRECTIVE.search(clauses[0])
                or not all(_DIRECTIVE.search(clause) or _CONTINUATION_DIRECTIVE.search(clause)
                           for clause in clauses[1:])):
            return False
    return bool(sentences)


def single_sentence_directive(text: str) -> bool:
    bounds = _boundaries(text)
    return (sum(bool(text[a:b].strip()) for a, b in zip(bounds, bounds[1:])) == 1
            and stable_user_directive(text))


class EvidenceContext:
    """Per-proposal source index; never a global cache of private turns."""
    def __init__(self, source: str):
        self.source = source
        self.bounds = evidence_boundaries(source)

    def mixed_intent(self, quotes: list[str]) -> bool:
        for quote in quotes:
            # This repair is a same-sentence coordination rule. Do not use an
            # intent anywhere in a long turn to normalize unrelated sentences
            # or replace established whole-record admission with a 3-unit cap.
            sentence_bounds = _boundaries(quote)
            for start, end in zip(sentence_bounds, sentence_bounds[1:]):
                sentence = quote[start:end]
                cuts = _independent_clause_cuts(sentence, [0, len(sentence)])
                if not cuts:
                    continue
                bounds = sorted({0, len(sentence), *cuts})
                units = [_DISCOURSE.sub('', sentence[a:b].strip())
                         for a, b in zip(bounds, bounds[1:])]
                if (any(_INTENT_OR_REQUEST.search(unit) for unit in units)
                        and any(_clause_head(unit) and not _INTENT_OR_REQUEST.search(unit) for unit in units)):
                    return True
        return False

    def clipped_restriction(self, quotes: list[str]) -> bool:
        for quote in quotes:
            context = self.context([quote]).strip()
            if context != quote.strip() and (
                    _ATTRIBUTED.search(context)
                    or _INTENT_OR_REQUEST.search(_DISCOURSE.sub('', context))
                    or _RESTRICTION_MARKER.findall(context.casefold())
                    != _RESTRICTION_MARKER.findall(quote.casefold())):
                # Exact text alone does not justify dropping the enclosing
                # conditional, attribution, question/plan or epistemic frame.
                return True
        return False

    def context(self, quotes: list[str]) -> str:
        source, bounds, contexts = self.source, self.bounds, []
        for quote in quotes:
            cursor = 0
            while (position := source.find(quote, cursor)) >= 0:
                cursor = position + 1
                lo = bisect_right(bounds, position) - 1
                hi = bisect_left(bounds, position + len(quote))
                start, end = bounds[lo], bounds[hi]
                while lo and _QUALIFIER_FRAGMENT.fullmatch(source[bounds[lo - 1]:start].strip()):
                    lo -= 1
                    start = bounds[lo]
                while hi + 1 < len(bounds) and _QUALIFIER_FRAGMENT.fullmatch(
                        source[end:bounds[hi + 1]].strip()):
                    hi += 1
                    end = bounds[hi]
                contexts.append(source[start:end])
        return '\n'.join(dict.fromkeys(contexts))


def quote_context(source: str, quotes: list[str]) -> str:
    """Keep original qualifiers, but not an independent sibling's intent."""
    return EvidenceContext(source).context(quotes)
