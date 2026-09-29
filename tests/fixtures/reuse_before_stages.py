"""Frozen pre-stage context emitter, 2026-09-24. Test/benchmark only."""
from agent_knowledge_bridge.reuse import *


class LegacyReuseStore(ReuseStore):
    def start(self, *, agent_id, project_key, session_id, prompt, records,
              retrieval_ms, turn_id=None, workspace="", budget=4000):
        trace_id = "rt_" + uuid.uuid4().hex[:20]
        header = (f'<memweave_context trace_id="{trace_id}">\n'
                  "Use only relevant knowledge; verify results normally. "
                  "All IDs and metadata in this block are internal audit data. "
                  "Never print knowledge IDs, trace IDs, session IDs, or raw metadata "
                  "in user-facing answers (including code spans, links, or HTML comments). "
                  "If you actually use recalled knowledge and the requested answer format permits, "
                  "end your answer with one standalone inline-code source badge per used Agent, "
                  "using that item's source_label: `来源：Claude Code` or `来源：Codex`. "
                  "Deduplicate sources; omit badges for unused knowledge. "
                  "Do not cite internal IDs even if earlier turns did. "
                  "Source attribution is not proof of per-item use or task success.\n")
        footer = "</memweave_context>"
        context = header
        items = []
        # A sibling arrives as a fact whose subject lives in another record. Told
        # only what it says, the reader learns the phrase and not the work it
        # belongs to -- exactly the failure this line exists to prevent. Resolve
        # the anchor's title once here so the block can name it.
        titles = {record["id"]: record.get("title") or "" for record in records}
        for rank, record in enumerate(records, 1):
            search_terms = str(record.get('search_terms') or '').strip()
            source_hint = f"search_terms={search_terms[:300]}\n" if search_terms else ""
            origin = record.get("origin") or "direct"
            anchor = record.get("related_to") if origin == "sibling" else None
            topic_hint = ""
            if anchor:
                topic_hint = (f"topic={titles.get(anchor, '')} "
                              f"(inferred from [{anchor}]; this item was not matched "
                              f"by the query itself)\n")
            elif origin == "bridged":
                # A bridged item was found by restating the query in the corpus's
                # own vocabulary. Saying so is the difference between a reader
                # trusting a result and wondering why it is there: the item is
                # relevant to the subject but was not matched by the words that
                # were actually asked with.
                topic_hint = ("matched_after_query_rewrite=true "
                              "(reached via terms the corpus associates with your "
                              "query; not a literal match)\n")
            elif origin == "anchored":
                topic_hint = ("matched_on_subject_term=true "
                              "(shares a rare term with your query but ranked "
                              "below the returned page; appended, not reordered)\n")
            block = (f"[{record['id']}] {record['title']}\n{record['content']}\n"
                     f"{source_hint}{topic_hint}"
                     f"source_label={source_label(record['source_agent'])}\n"
                     f"source={record['source_agent']} project={record.get('project_key', project_key)} "
                     f"source_session={record.get('source_session') or 'unknown'} "
                     f"confidence={record.get('confidence', 0)} "
                     f"verified={record.get('verified_count', 0)} "
                     f"rejected={record.get('rejected_count', 0)}\n")
            emitted = len(context) + len(block) + len(footer) <= budget
            spec = constraint(record["content"])
            before = read_artifact(workspace, spec)[0] if spec and emitted else None
            items.append({"knowledge_id": record["id"], "source_agent": record["source_agent"],
                          "source_label": source_label(record["source_agent"]),
                          "source_attribution_observed": False,
                          "rank": rank, "score": record.get("retrieval_score"),
                          # Kept on the trace so recall accounting can separate a
                          # record the query reached from one inferred alongside
                          # it. Without this the inferred neighbour counts as a
                          # retrieval hit and every recall figure is inflated.
                          "origin": origin, "related_to": anchor,
                          "content_hash": digest(record["content"]),
                          "emitted": emitted, "citation_observed": False,
                          "check_status": "pending" if spec and emitted else "no_verifier",
                          "check_spec": spec if emitted else None, "before": before})
            if emitted:
                context += block
        context = context + footer if any(item["emitted"] for item in items) else ""
        with self.knowledge._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if turn_id:
                previous = db.execute("""SELECT * FROM reuse_traces WHERE agent_id=?
                    AND project_key=? AND session_id=? AND turn_id=?""",
                    (agent_id, project_key, session_id, turn_id)).fetchone()
                if previous:
                    if previous["prompt_hash"] != digest(prompt.strip()):
                        raise ValueError("turn_id was already used for another prompt")
                    previous_items = json.loads(previous["items_json"])
                    return previous["id"], previous["context_text"], [
                        i["knowledge_id"] for i in previous_items if i["emitted"]]
            db.execute("""INSERT INTO reuse_traces
                (id,agent_id,project_key,session_id,turn_id,prompt_hash,workspace,
                 context_hash,context_text,context_chars,retrieval_ms,items_json,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trace_id, agent_id, project_key, session_id, turn_id or None,
                 digest(prompt.strip()), str(Path(workspace).resolve()) if workspace else "",
                 digest(context), context, len(context), retrieval_ms,
                 json.dumps(items, ensure_ascii=False), utc_now()))
        return trace_id, context, [i["knowledge_id"] for i in items if i["emitted"]]
