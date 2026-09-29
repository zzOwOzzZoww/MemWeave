"""Filesystem locations MemWeave resolves without needing any other module.

Everything here is stdlib-only and import-free within the package, because the
hook scripts import it before the rest of MemWeave is on the path and the
daemon resolves its database before it builds any service.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


#: Repository root, derived from this file's location under ``src/``.
PACKAGE_PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: The database name used inside ``<home>/data`` and inside a checkout.
DATABASE_NAME = "knowledge.db"


def memweave_home() -> Path:
    configured = os.getenv("MEMWEAVE_HOME") or os.getenv("MW_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".memweave"


def configured_database_path() -> Path | None:
    """Return the database ``memweave init`` recorded, if it has run.

    The backup written before a config rewrite is read too: it is what makes an
    interrupted ``memweave init --force`` recoverable without pointing the
    Runtime at a database that does not exist yet.
    """
    for name in ("config.json", "config.json.memweave-backup"):
        path = memweave_home() / name
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(config, dict):
            continue
        candidate = config.get("database_path")
        if isinstance(candidate, str) and candidate.strip():
            return Path(candidate).expanduser().resolve()
    return None


def default_database_path(*, repo_root: Path | None = None) -> Path:
    """Resolve the database to open when no caller supplied one.

    One rule, applied identically by the daemon, the service and both hooks:

    1. ``MW_DB_PATH`` / ``AKB_DB_PATH`` — an explicit choice always wins.
    2. The database recorded by ``memweave init``, or by a running Runtime.
       A recorded value is the user's stated intent and must outrank any
       guess made from where the code happens to live.
    3. ``<home>/data/knowledge.db`` — a first run with no prior state.
    4. A database already present inside the checkout.  Only reached when
       nothing has been recorded, so it keeps a development tree that has
       been writing to its own file working instead of silently opening a
       fresh, empty database elsewhere.

    The daemon previously reached for (3) directly while the hooks fell back
    to (4), so a daemon started without an explicit path served a different
    database than the hooks it was feeding.  Both now share this function.
    """
    for name in ("MW_DB_PATH", "AKB_DB_PATH"):
        configured = os.getenv(name)
        if configured and configured.strip():
            return Path(configured).expanduser().resolve()

    recorded = configured_database_path()
    if recorded is not None:
        return recorded

    home_database = (memweave_home() / "data" / DATABASE_NAME).resolve()
    if home_database.exists():
        return home_database

    root = PACKAGE_PROJECT_ROOT if repo_root is None else repo_root
    in_tree = (Path(root).expanduser().resolve() / "data" / DATABASE_NAME)
    if in_tree.exists():
        return in_tree

    # Nothing recorded and nothing to preserve: pick the stable location.
    return home_database


#: Every hook and both agents ask for the same answer under the same name.
database_path = default_database_path
