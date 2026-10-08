from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent_knowledge_bridge.claude_transcript import (
    TranscriptTurn,
    redact_text,
)
from agent_knowledge_bridge.compiler import CompilationIdentity
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.reuse import ReuseStore, digest
from agent_knowledge_bridge.decisions import grounded_user_proposal, uncertain_only
from agent_knowledge_bridge.experiences import (
    validate_contract, encode_contract, admission_evidence, phrase_in,
)


REVIEW_SYSTEM_PROMPT = """You are the review component of a continual-learning system.
Extract zero to three durable, reusable lessons from a completed coding-agent turn.

Return one JSON object with this exact shape:
{"proposals":[{"title":"...","content":"...","search_terms":"...","subject_terms":["..."],"knowledge_type":"fact|preference|procedure|decision","scope":"user|project","evidence_event_ids":["ae_..."]}]}

Rules:
- Return an empty proposals array when the turn contains no reusable lesson.
- Save procedures, stable project facts, explicit preferences, or durable decisions.
- Set scope to `user` only for a durable, project-independent rule or preference that
  another project can safely reuse; use `project` for repository, service, or task
  specific knowledge. When uncertain, choose `project`.
- Do not save incident narration, task-specific output, credentials, private conversation, or guesses.
- A failure may yield a bounded, reusable caution/procedure candidate. State only the observed failure; do not invent a root cause or successful remedy.
- State procedures as instructions that a future agent can execute.
- Add 6 to 12 compact Chinese and English retrieval terms or short aliases in search_terms.
- search_terms is index metadata, not a place for instructions, evidence, or secrets.
- When the lesson is about a named person, service, repository or other entity,
  add up to 16 exact subject_terms. Use only names explicitly present in the
  source turn; leave it empty when the subject is ambiguous. These terms are
  used to prevent evidence about a different subject from being injected.
- Cite only EVENT_ID values present in the input.
- Cite a successful objective test event only when it actually supports the proposal.
- Never invent an event ID or claim a test passed when SUCCESS is not true.
- For ordinary proposals add source_quotes: an array of 1-3 EXACT verbatim
  excerpts from USER REQUEST, and source_role="user". Preserve negation, scope and conditions. If the
  user explicitly asks to remember one durable preference/decision, quote their
  entire request. Do not convert a question, an unverified assistant guess or
  generic advice into a lesson. If no user assertion supports ordinary knowledge,
  return zero proposals. Observed tool failure/recovery uses experience below.
- Do not discard a directly asserted durable user fact or preference merely
  because the same sentence also asks a question; quote only the assertion.
  Do not infer durable facts from the question itself or transient activity.
- With actual TOOL EVIDENCE, an ordinary observation may instead quote
  ASSISTANT RESULT with source_role="assistant"; this always awaits review.
- Do not expand a quote with plausible but unobserved steps. The stored ordinary
  body is built from exact quotes; your content is only a proposed summary.

For a reusable failure/recovery procedure, optionally add an "experience" object
to a proposal (knowledge_type="procedure", scope="project"):
{"version":1,"applies_when":["specific task phrase"],"exclude_when":["incompatible environment"],
 "steps":["Check the precondition","Apply the observed correction"],
 "avoid":["The observed failing action"],"reason":"What was actually observed",
 "verifier":{"kind":"observed_command","command":"exact command from tool INPUT"}}
Use 1-6 precise applicability phrases, all present in the source material. They
must ALL match a future request; do not use generic phrases like "task" alone.
Use up to 6 explicit exclusions only when supported by evidence. A verifier must
be an exact observed test/check command, not an invented command. Cite both the
failure and the later successful rerun when present. Unresolved failures remain
candidates. A matching fail-then-pass validates a recovery observation, not causal
attribution of success to your proposed steps. Omit experience when no specific
conditions/verifier can be justified; ordinary knowledge extraction still applies.
"""

SECRET_STORAGE_REQUEST = re.compile(
    r"(?i)(?:记住|保存|存储|remember|save|store).{0,50}"
    r"(?:api[ _-]?key|密钥|密码|口令|password|token|credential|secret)"
)


class DeepSeekReviewer:
    def __init__(self) -> None:
        from .provider import settings
        config = settings()
        self.api_key = config['api_key']
        self.base_url = config['base_url'].rstrip('/')
        self.model = config['model']
        self.timeout = min(120.0, max(1.0, float(os.getenv("MW_REVIEW_TIMEOUT", "90"))))

    def __call__(self, review_text: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("学习 API 未配置，请运行 memweave setup")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": redact_text(review_text, len(review_text))},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            from .provider import open_request
            with open_request(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"review model HTTP {exc.code}; check provider configuration and quota") from None
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("review model returned no message content") from exc
        parsed = self._parse_json(content)
        proposals = parsed.get('proposals', [])
        if isinstance(proposals, list):
            parsed['proposals'] = [p for p in proposals if isinstance(p, dict)
                                   and ('experience' in p or 'source_quotes' in p)]
        return parsed

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Parse a review response without making harmless wrapper text fatal.

        DeepSeek's JSON mode normally returns a bare object, but some gateway
        paths still add a short explanation or Markdown code fence.  A strict
        ``json.loads`` turned those responses into a failed learning run even
        though the object itself was valid.  Decode the first complete JSON
        object while keeping the response shape validation intact.
        """
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 2 and lines[-1].strip().startswith("```"):
                text = "\n".join(lines[1:-1]).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as original:
            decoder = json.JSONDecoder()
            parsed = None
            # Find a JSON object embedded in a short preamble or followed by
            # explanatory text. raw_decode handles nested objects and braces
            # inside quoted strings correctly.
            for offset, character in enumerate(text):
                if character != "{":
                    continue
                try:
                    candidate, _end = decoder.raw_decode(text[offset:])
                except json.JSONDecodeError:
                    continue
                parsed = candidate
                break
            if parsed is None:
                raise original
        if not isinstance(parsed, dict):
            raise RuntimeError("review model response must be a JSON object")
        return parsed


class LearningEngine:
    def __init__(
        self,
        *,
        database_path: Path,
        agent_id: str,
        project_key: str,
        reviewer: Callable[[str], dict[str, Any]] | None = None,
        transcript_parser: Callable[..., TranscriptTurn] | None = None,
        bind_transcript_boundary: bool = False,
    ) -> None:
        self.store = LearningStore(database_path)
        self.reuse = ReuseStore(database_path)
        self.agent_id = agent_id
        self.project_key = project_key
        # Recall never needs API credentials or provider configuration I/O.
        self.reviewer = reviewer or (lambda text: DeepSeekReviewer()(text))
        self.transcript_parser = transcript_parser
        self.bind_transcript_boundary = bind_transcript_boundary
        self._governor: Governor | None = None

    def recall_context(self, hook_input: dict[str, Any]) -> str:
        if not self.store.knowledge.agent_allowed(self.agent_id):
            return ""
        prompt = str(hook_input.get("prompt") or "").strip()
        session_id = str(hook_input.get("session_id") or "unknown-session")
        if not prompt:
            return ""
        started = time.perf_counter()
        turn_id = hook_input.get("turn_id") or (
            "prompt:" + str(hook_input["prompt_id"]) if hook_input.get("prompt_id") else None)
        boundary = None
        if self.bind_transcript_boundary and self.transcript_parser and hook_input.get("transcript_path"):
            boundary = {"key": None, "fresh": False}
            try:
                current = self.transcript_parser(hook_input["transcript_path"], max_bytes=65_536)
                # Bind only a fresh, exact prompt with a transcript identity.
                # A repeated old prompt with completed work must not receive credit.
                fresh = (current.user_text.strip() == prompt and not current.tools
                         and not current.assistant_text)
                boundary = {"key": current.source_turn_key, "fresh": fresh}
                if not turn_id and fresh:
                    turn_id = current.source_turn_key
            except (OSError, ValueError):
                pass  # Missing/not-yet-flushed transcripts keep weak association.
        existing = self.reuse.existing(self.agent_id, self.project_key, session_id, turn_id)
        if existing:
            if existing["prompt_hash"] != digest(prompt):
                raise ValueError("turn_id was already used for another prompt")
            context = self.reuse.live_context(existing)
            return context
        recall_limit = int(os.getenv("MW_RECALL_LIMIT", "3"))
        result = self.store.knowledge.search(
            requester_agent=self.agent_id,
            project_key=self.project_key,
            query=prompt[:500],
            limit=recall_limit,
        )
        normal_records = result['results']
        from .retrieval_pipeline import RetrievalPolicy, order_recovery_candidates
        from .decisions import query_intent
        recovered = self._recover_for_query(prompt, limit=recall_limit)
        records = order_recovery_candidates(normal_records, recovered,
                                           query=query_intent(prompt[:500], project_key=self.project_key).focus)
        latency_ms = (time.perf_counter() - started) * 1000
        reuse_args = dict(
            agent_id=self.agent_id, project_key=self.project_key, session_id=session_id,
            prompt=prompt, retrieval_ms=latency_ms,
            turn_id=turn_id, workspace=str(hook_input.get("cwd") or ""),
            transcript_boundary=boundary,
            validate_live_records=True, record_hits=True,
            record_limit=recall_limit + RetrievalPolicy().slack,
        )
        try:
            _, context, emitted_ids = self.reuse.start(records=records, **reuse_args)
        except Exception:
            if not recovered:
                raise
            # The restoration and trace transaction has rolled back. A failed
            # optional recovery must not prevent normal knowledge from serving.
            _, context, emitted_ids = self.reuse.start(records=normal_records, **reuse_args)
        self.store.record_recall(
            agent_id=self.agent_id,
            project_key=self.project_key,
            session_id=session_id,
            query="sha256:" + digest(prompt),
            knowledge_ids=emitted_ids,
            injected_chars=len(context),
            latency_ms=latency_ms,
        )
        # Actual emission counts now commit with restoration and the trace;
        # a duplicate hook cannot increment them or resurrect a record twice.
        return context

    def _recall_governor(self):
        if os.getenv('MW_LFHV_PROBE', '1') != '1':
            return None
        with self.store.knowledge._connect() as db:
            retired = db.execute(
                "SELECT 1 FROM knowledge_records WHERE status='archived' "
                "AND (scope='user' OR project_key=?) LIMIT 1", (self.project_key,),
            ).fetchone()
        if retired is None:
            return None
        if self._governor is None:
            self._governor = Governor(self.store.knowledge)
        return self._governor

    def _recover_for_query(self, prompt: str, *, limit: int) -> list[dict[str, Any]]:
        if os.getenv('MW_LFHV_RECOVERY', '1') != '1':
            self._shadow_probe(prompt)
            return []
        if limit <= 0:
            return []
        try:
            governor = self._recall_governor()
            if governor is None:
                return []
            candidates = governor.prepare_recovery(project_key=self.project_key,
                query=prompt[:500], requester_agent=self.agent_id, limit=max(8, limit))
            return candidates
        except Exception:
            # An unavailable recovery path leaves ordinary recall usable.
            return []

    def _shadow_probe(self, prompt: str) -> None:
        """Keep retirement falsifiable by asking what it would have cost.

        A probe is skipped unless the project actually holds retired records, so
        the usual cost is one bounded existence check per turn. Nothing the probe finds is
        injected into the agent's context; it only records that a retired record
        was still being asked for. This observation-only path is retained for
        MW_LFHV_RECOVERY=0 and offline comparisons.
        """
        try:
            governor = self._recall_governor()
            if governor is not None:
                governor.shadow_probe(project_key=self.project_key, query=prompt[:500])
        except Exception:
            # Governance observes recall; it must never be able to break it. A
            # failed probe loses one measurement, not the turn.
            pass

    def _parse_turn(self, hook_input: dict[str, Any]) -> TranscriptTurn:
        if self.transcript_parser is None:
            raise ValueError("learning requires normalized turn data or a transcript parser")
        return self.transcript_parser(
            hook_input.get("transcript_path") or "",
            fallback_assistant=str(hook_input.get("last_assistant_message") or ""),
            **({'end_offset': hook_input['transcript_end']} if hook_input.get('transcript_end') is not None else {}),
        )

    def learn(self, hook_input: dict[str, Any]) -> dict[str, Any]:
        if not self.store.knowledge.agent_allowed(self.agent_id):
            return {'status': 'disabled'}
        started = time.perf_counter()
        session_id = str(hook_input.get("session_id") or "unknown-session")
        turn = self._parse_turn(hook_input)
        compilation = CompilationIdentity.from_turn(
            agent_id=self.agent_id,
            project_key=self.project_key,
            session_id=session_id,
            turn_hash=turn.turn_hash,
        )
        run_id = self.store.begin_run(
            agent_id=self.agent_id,
            project_key=self.project_key,
            session_id=session_id,
            turn_hash=compilation.manifest_hash,
            input_chars=len(turn.user_text) + len(turn.assistant_text),
            source_hash=compilation.source_hash,
            compiler_version=compilation.compiler_version,
            schema_version=compilation.schema_version,
        )
        if run_id is None:
            with self.store.knowledge._connect() as db:
                prior = db.execute('SELECT status FROM learning_runs WHERE turn_hash=?',
                    (compilation.manifest_hash,)).fetchone()
            return {
                "status": "duplicate_turn",
                "run_status": prior['status'] if prior else 'unknown',
                "reason": "source, compiler, and schema are unchanged",
                "compiler": {
                    "source_hash": compilation.source_hash,
                    "compiler_version": compilation.compiler_version,
                    "schema_version": compilation.schema_version,
                },
            }

        event_ids = self._record_events(session_id, turn)
        successful_objective = [
            event_id
            for event_id, tool in zip(event_ids, turn.tools, strict=True)
            if tool.success is True and tool.objective_kind
        ]
        failed_objective = any(
            tool.success is False and tool.objective_kind for tool in turn.tools
        )
        self.reuse.complete(
            agent_id=self.agent_id, project_key=self.project_key, session_id=session_id,
            turn=turn, turn_id=hook_input.get("turn_id") or (
                "prompt:" + str(hook_input["prompt_id"]) if hook_input.get("prompt_id") else None
            ) or turn.source_turn_key,
        )
        # Legacy counters describe turn-level command outcomes only, never item adoption.
        self.store.complete_latest_recall(
            session_id, agent_id=self.agent_id, project_key=self.project_key,
            successful_validation=(False if failed_objective else True if successful_objective else None),
        )

        proposal_count = 0
        reviewer_proposal_count = 0
        rejected_proposal_count = 0
        proposal_outcome = "unknown"
        promoted_count = 0
        try:
            # A credential-storage request is not evidence for a new policy,
            # even if a reviewer could rephrase the refusal as a general lesson.
            secret_only = not turn.tools and bool(SECRET_STORAGE_REQUEST.search(turn.user_text))
            if secret_only:
                response = {"proposals": []}
                proposal_outcome = "suppressed_sensitive_request"
            elif uncertain_only(turn):
                response = {"proposals": []}
                proposal_outcome = "suppressed_unverified_claim"
            else:
                response = self.reviewer(turn.review_text(event_ids))
            proposals = response.get("proposals", [])
            if not isinstance(proposals, list):
                raise RuntimeError("review response proposals must be a list")
            reviewer_proposal_count = len(proposals)
            for proposal in proposals[:3]:
                outcome = self._persist_proposal(
                    proposal,
                    run_id=run_id,
                    session_id=session_id,
                    available_event_ids=event_ids,
                    source_text=turn.review_text(event_ids),
                    source_turn=turn,
                )
                if outcome is None:
                    continue
                proposal_count += 1
                promoted_count += int(outcome)
            rejected_proposal_count = max(0, reviewer_proposal_count - proposal_count)
            if proposal_outcome == "unknown":
                if not reviewer_proposal_count:
                    proposal_outcome = "reviewer_returned_zero"
                elif not proposal_count:
                    proposal_outcome = "all_rejected"
                elif rejected_proposal_count:
                    proposal_outcome = "partial"
                else:
                    proposal_outcome = "accepted"
            self.store.finish_run(
                run_id,
                status="completed",
                proposal_count=proposal_count,
                reviewer_proposal_count=reviewer_proposal_count,
                rejected_proposal_count=rejected_proposal_count,
                proposal_outcome=proposal_outcome,
                promoted_count=promoted_count,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            self.store.finish_run(
                run_id,
                status="failed",
                proposal_count=proposal_count,
                reviewer_proposal_count=reviewer_proposal_count,
                rejected_proposal_count=rejected_proposal_count,
                proposal_outcome="processing_failed",
                promoted_count=promoted_count,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=redact_text(str(exc), 1000),
            )
            raise
        return {
            "status": "completed",
            "proposals": proposal_count,
            "reviewer_proposals": reviewer_proposal_count,
            "rejected_proposals": rejected_proposal_count,
            "proposal_outcome": proposal_outcome,
            "promoted": promoted_count,
            "compiler": {
                "run_id": run_id,
                "source_hash": compilation.source_hash,
                "compiler_version": compilation.compiler_version,
                "schema_version": compilation.schema_version,
            },
        }

    def _record_events(self, session_id: str, turn: TranscriptTurn) -> list[str]:
        event_ids: list[str] = []
        for tool in turn.tools:
            event_ids.append(
                self.store.record_event(
                    agent_id=self.agent_id,
                    project_key=self.project_key,
                    session_id=session_id,
                    event_type="tool_result",
                    payload={
                        "tool_use_id": tool.tool_use_id,
                        "tool_name": tool.tool_name,
                        "input": tool.input_summary,
                        "output": tool.output_summary,
                    },
                    success=tool.success,
                    objective_kind=tool.objective_kind,
                )
            )
        return event_ids

    def _persist_proposal(
        self,
        proposal: Any,
        *,
        run_id: str,
        session_id: str,
        available_event_ids: list[str],
        source_text: str = "",
        source_turn: TranscriptTurn | None = None,
    ) -> bool | None:
        if not isinstance(proposal, dict):
            return None
        if not self.store.knowledge.agent_allowed(self.agent_id):
            return None
        title = str(proposal.get("title") or "").strip()
        content = str(proposal.get("content") or "").strip()
        knowledge_type = str(proposal.get("knowledge_type") or "").strip()
        scope = str(proposal.get("scope") or "project").strip()
        search_terms = str(proposal.get("search_terms") or "").strip()
        subject_terms = proposal.get("subject_terms") or []
        if not isinstance(subject_terms, list) or len(subject_terms) > 16:
            return None
        subject_terms = [str(item).strip() for item in subject_terms
                         if isinstance(item, str) and 0 < len(item.strip()) <= 100]
        grounded = None
        if source_turn is not None and 'experience' not in proposal:
            try:
                grounded = grounded_user_proposal(proposal, source_turn)
            except ValueError:
                return None
            if grounded:
                content, scope = grounded['content'], grounded['scope']
        if not title or not content or knowledge_type not in {
            "fact",
            "preference",
            "procedure",
            "decision",
        }:
            return None
        if len(search_terms) > 1000:
            return None
        if scope not in {"user", "project"}:
            scope = "project"
        if redact_text(title, len(title) + 1) != title or redact_text(
            content, len(content) + 1
        ) != content or redact_text(search_terms, len(search_terms) + 1) != search_terms:
            return None
        if any(redact_text(item, len(item) + 1) != item for item in subject_terms):
            return None
        cited = proposal.get("evidence_event_ids") or []
        if not isinstance(cited, list):
            cited = []
        cited_ids = [
            str(event_id)
            for event_id in cited
            if str(event_id) in set(available_event_ids)
        ]
        experience = None
        recovery = None
        if content.startswith('{"experience":') and 'experience' not in proposal:
            return None
        if "experience" in proposal:
            try:
                experience = validate_contract(proposal["experience"])
            except (ValueError, TypeError):
                return None
            event_map = self.store.get_events(available_event_ids)
            # Source evidence must belong to this agent/project/session. The
            # model cannot use another session's passing test as admission.
            event_map = {key: event for key, event in event_map.items()
                         if event['agent_id'] == self.agent_id
                         and event['project_key'] == self.project_key
                         and event['session_id'] == session_id}
            from agent_knowledge_bridge.experiences import command_from_summary
            observed = [event for event in event_map.values()
                        if event['objective_kind'] in {'test', 'command'}
                        and command_from_summary(event['payload'].get('input', ''))
                        == experience['verifier']['command']]
            if not observed or not all(phrase_in(term, source_text) for term in experience['applies_when'] + experience['exclude_when']):
                return None
            recovery = admission_evidence(experience, event_map, available_event_ids, cited_ids)
            content = encode_contract(experience)
            scope, knowledge_type = 'project', 'procedure'
            search_terms = ' '.join(experience['applies_when']) + (' ' + search_terms if search_terms else '')
            search_terms = search_terms[:1000]
        # Existing related accepted knowledge makes automatic acceptance unsafe:
        # leave the new statement for conflict review. This runs on the write
        # path only and is intentionally conservative about possible conflicts.
        auto_accept = bool(grounded and grounded['auto_accept'])
        if auto_accept:
            related = self.store.knowledge.search(requester_agent=self.agent_id,
                project_key=self.project_key, query=title[:500], limit=3, expand_siblings=False)
            auto_accept = not related['results']
        published = self.store.knowledge.publish(
            source_agent=self.agent_id,
            project_key=self.project_key,
            title=title,
            content=content,
            knowledge_type=knowledge_type,
            scope=scope,
            evidence_summary=f"Automatically proposed by the {self.agent_id} post-turn review.",
            source_session=session_id,
            evidence_speaker=(
                getattr(source_turn, "speaker", None)
                if source_turn is not None else None
            ),
            search_terms=search_terms or None,
            subject_terms=subject_terms,
        )
        knowledge_id = published["knowledge"]["id"]
        self.store.link_compilation(
            run_id,
            knowledge_id,
            "deduplicated" if published["deduplicated"] else "produced",
        )
        for event_id in cited_ids:
            self.store.link_knowledge(knowledge_id, event_id, "supports")

        if auto_accept and published['knowledge']['status'] == 'candidate' and not published['deduplicated']:
            feedback = self.store.knowledge.feedback(
                agent_id=self.agent_id, knowledge_id=knowledge_id, outcome='verified',
                evidence_kind='user_approval', evidence_ref='explicit-user-statement:' + digest(content),
                evidence_summary='Exact user statement explicitly requests durable storage; no related accepted record found.',
                project_key=self.project_key, require_no_related=True)
            return feedback['knowledge']['status'] == 'active'

        if experience:
            with self.store.knowledge._connect() as db:
                db.execute('''INSERT OR IGNORE INTO experience_outcomes
                    (knowledge_id,admission,failed_event,passed_event,updated_at) VALUES (?,?,?,?,?)''',
                    (knowledge_id, 'recovery_observed' if recovery else 'needs_recovery_evidence',
                     recovery['failed_event'] if recovery else None,
                     recovery['passed_event'] if recovery else None, self.store.knowledge.clock()))
                if recovery and published['knowledge']['status'] == 'candidate':
                    db.execute('''UPDATE experience_outcomes SET admission='recovery_observed',
                        failed_event=?,passed_event=?,updated_at=? WHERE knowledge_id=?''',
                        (recovery['failed_event'], recovery['passed_event'], self.store.knowledge.clock(), knowledge_id))
            # A single success must never launder an unsupported failure lesson.
            if not recovery or published['knowledge']['status'] != 'candidate':
                return False
            feedback = self.store.knowledge.feedback(
                agent_id=self.agent_id, knowledge_id=knowledge_id, outcome='verified',
                evidence_summary='Same verifier failed then passed in the source turn; recovery observed, causal remedy unproven.',
                evidence_kind='test', evidence_ref=f"agent-event:{recovery['failed_event']}->{recovery['passed_event']}",
                project_key=self.project_key)
            return feedback['knowledge']['status'] == 'active'

        # A successful command proves its outcome, not every model inference
        # from that turn. Ordinary proposals await review; the typed experience
        # contract above has its own deterministic admission validator.
        return False
