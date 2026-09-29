"""Stable identities for incremental knowledge compilation.

Raw Agent events remain immutable.  A compilation identity binds the raw turn
to the compiler and schema versions, so unchanged input is skipped while a
compiler or schema upgrade can deliberately rebuild the same source material.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


COMPILER_VERSION = "memweave-compiler-v3-grounded-admission"
SCHEMA_VERSION = "memory-record-v1"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompilationIdentity:
    source_hash: str
    manifest_hash: str
    compiler_version: str
    schema_version: str

    @classmethod
    def from_turn(
        cls,
        *,
        agent_id: str,
        project_key: str,
        session_id: str,
        turn_hash: str,
        compiler_version: str = COMPILER_VERSION,
        schema_version: str = SCHEMA_VERSION,
    ) -> "CompilationIdentity":
        source_hash = _digest(agent_id, project_key, session_id, turn_hash)
        return cls(
            source_hash=source_hash,
            manifest_hash=_digest(source_hash, compiler_version, schema_version),
            compiler_version=compiler_version,
            schema_version=schema_version,
        )
