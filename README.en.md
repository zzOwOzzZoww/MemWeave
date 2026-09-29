# MemWeave

[简体中文](https://github.com/zzOwOzzZoww/MemWeave/blob/main/README.md) | **English**

MemWeave is a local shared-memory layer for coding agents.

It lets agents such as Claude Code and Codex reuse confirmed project knowledge: technical decisions, user preferences, lessons learned, and working agreements. It does not treat every conversation as permanent memory, and it does not inject loosely related content just to make recall numbers look better.

> Current version: 0.5.0a1 (Alpha). The core loop is working and ready for evaluation in test projects.

## What problem does it solve?

Switching between coding agents often creates the same problems:

- Codex learns a project rule, but Claude Code has to ask again.
- An old decision has been replaced, but an agent still uses it.
- A casual statement becomes “permanent truth” and keeps affecting later work.
- A recall system injects vaguely similar notes even when they are not useful.

MemWeave focuses on a small, complete loop:

1. After an agent finishes a turn, it extracts only possible knowledge candidates.
2. A candidate becomes usable only after human approval or objective evidence.
3. A new task receives only knowledge relevant to the current project and query.
4. Old knowledge can be archived, quarantined, replaced, or deleted while keeping its provenance.

## Design principles

- **Local first**: the knowledge database and Runtime run locally by default.
- **Candidate before active**: new knowledge starts as candidate and cannot silently pollute long-term memory.
- **Zero recall is normal**: when nothing is relevant, MemWeave returns nothing.
- **Core does not depend on MCP**: agents connect through native hooks or the Runtime API; MCP is optional.
- **Context stays attached**: project scope, source agent, source session, and evidence references are preserved.
- **Sensitive data does not belong in memory**: do not store passwords, tokens, raw private conversations, or unverified guesses.

## How it works

~~~text
Claude Code Hook ─┐
Codex Hook ───────┼─ Local Runtime ── retrieve → arbitrate → inject context
CLI / Web ───────┘       │
                        └─ background learning → candidates → review/evidence → lifecycle
                                                │
                                           SQLite + FTS5

Optional MCP ───────────────────────────────────┘
~~~

The retrieval hot path does not call a model. It starts with SQLite FTS5/BM25, then applies limited bilingual term bridges, topic expansion, and evidence gates. MemWeave is not trying to be a general-purpose semantic search engine; it is designed to keep a focused knowledge-governance loop explainable and auditable.

## Quick start

Python 3.11 or newer is required.

### Install directly from GitHub

~~~shell
python -m pip install "git+https://github.com/zzOwOzzZoww/MemWeave.git"
memweave setup
~~~

### Install from source (for developers)

~~~shell
git clone https://github.com/zzOwOzzZoww/MemWeave.git
cd MemWeave
python -m pip install -e .
memweave setup
~~~

setup asks for the model provider Base URL, model name, and API key. The model is used only for background knowledge extraction. Normal recall does not read credentials or call a model.

On Windows, setup creates a “MemWeave Knowledge Manager” desktop shortcut. On macOS and Linux, use memweave ui; desktop shortcut behavior has only been validated on Windows so far.

## Common commands

~~~shell
memweave                     # configure on first run, then open the management UI
memweave ui                  # start the Runtime and open the UI
memweave status              # inspect agents, knowledge, and learning jobs
memweave configure           # update model API settings without deleting knowledge
memweave doctor              # check local configuration and runtime health
memweave doctor --check-api  # send a small API request; this may have a small cost
memweave shortcut            # repair the Windows desktop shortcut
~~~

Use “Agent Maintenance” in the management UI to enable Claude Code or Codex. This installs the corresponding global hook, and the client may need to be restarted afterward.

## When does knowledge become active?

MemWeave does not treat “the model said it” as proof. Common states include:

- **candidate**: newly extracted and waiting for review.
- **active**: approved or verified and available for recall.
- **archived**: kept for history but normally excluded from recall.
- **quarantined**: risky or expired during review and excluded by default.

When a clear preference or decision changes, the UI can show the conflict between old and new versions. After replacement is confirmed, the new version becomes active and the old one remains only for history. Complex, conditional, or ambiguous statements still require human judgment.

## Data and privacy

The default data directory is ~/.memweave. It contains configuration, the SQLite database, runtime state, and logs. Set MEMWEAVE_HOME to use another location.

Background extraction sends a redacted conversation summary to the model provider you configure. Redaction cannot cover every custom credential format, so do not connect conversations that must never leave the machine to a remote provider.

API keys are not stored in the knowledge database. Windows uses per-user DPAPI encryption. Other systems currently store the key in a separate file with 0600 permissions, without additional encryption.

## Development and tests

~~~shell
python -m pip install -e ".[test]"
python -m pytest tests -q
python -m pip wheel . --no-deps --wheel-dir dist
~~~

As of 2026-09-29, the full suite reports **408 passed, 9 subtests passed**, with CI coverage on Windows, Ubuntu, Python 3.11, and Python 3.12. These results validate the fixed test suites only; they are not claims about open-domain understanding, real-agent task success, or production-scale performance.

Project layout:

~~~text
src/agent_knowledge_bridge/        Core, Runtime, CLI, and management UI
src/agent_knowledge_bridge/hooks/  Native Claude Code and Codex hooks
scripts/                           Development, installation, and evaluation tools
tests/                             Regression and product-installation tests
docs/                              Design notes and historical validation records
evaluation/                        Candidate synthetic evaluation dataset
~~~

## Current limits

- This is an Alpha release and has not been validated across every OS and client version.
- The bilingual term bridge is a small, auditable rule set, not a universal language model.
- Synthetic evaluations test closed-loop behavior; they do not represent real conversations or real tasks.

This project is licensed under the [Apache License 2.0](https://github.com/zzOwOzzZoww/MemWeave/blob/main/LICENSE).

See [docs/](https://github.com/zzOwOzzZoww/MemWeave/tree/main/docs) for detailed design and validation notes.
