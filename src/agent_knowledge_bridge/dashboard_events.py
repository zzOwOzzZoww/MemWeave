"""Bounded, cross-process UI invalidation for local hooks and runtime writes."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .learning import LearningStore


class DashboardChanges:
    def __init__(self, learning: LearningStore) -> None:
        self.store = learning.knowledge
        with self.store._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS dashboard_revision (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO dashboard_revision(singleton) VALUES (1);
            ''')
            # Database triggers also observe hooks running outside this process.
            for table in ('learning_runs', 'knowledge_compilation_links', 'knowledge_evidence'):
                for event in ('INSERT', 'UPDATE', 'DELETE'):
                    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS mw_ui_{table}_{event.lower()}
                        AFTER {event} ON {table} BEGIN
                        UPDATE dashboard_revision SET revision=revision+1 WHERE singleton=1;
                        END''')
            for event in ('INSERT', 'DELETE'):
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS mw_ui_agents_{event.lower()}
                    AFTER {event} ON agent_registry BEGIN
                    UPDATE dashboard_revision SET revision=revision+1 WHERE singleton=1;
                    END''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS mw_ui_agents_update
                AFTER UPDATE ON agent_registry
                WHEN OLD.enabled IS NOT NEW.enabled OR OLD.display_name IS NOT NEW.display_name
                  OR OLD.adapter_type IS NOT NEW.adapter_type OR OLD.capabilities_json IS NOT NEW.capabilities_json
                BEGIN UPDATE dashboard_revision SET revision=revision+1 WHERE singleton=1; END''')

    def revision(self, connection) -> tuple[str, str, int]:
        row = connection.execute('''SELECT corpus.identity, corpus.epoch, ui.revision
            FROM retrieval_revision corpus CROSS JOIN dashboard_revision ui
            WHERE corpus.singleton=1 AND ui.singleton=1''').fetchone()
        return tuple(row)

    async def stream(self, request, *, interval: float = 0.5) -> AsyncIterator[str]:
        last = None
        idle = 0
        with self.store._connect() as connection:
            while not await request.is_disconnected():
                revision = self.revision(connection)
                if revision != last:
                    last = revision
                    idle = 0
                    yield 'event: changed\ndata: {}\n\n'
                else:
                    idle += interval
                    if idle >= 15:
                        idle = 0
                        yield ': keepalive\n\n'
                await asyncio.sleep(interval)
