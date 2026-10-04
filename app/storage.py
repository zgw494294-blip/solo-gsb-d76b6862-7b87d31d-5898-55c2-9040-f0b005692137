"""SQLite-backed persistence for graphs, the shared registry and packages.

Three pieces of state survive restarts:

* the shared sensitive-column registry (entries keyed by ``(source, column)``)
  together with its monotonically increasing ``generation`` (0 on a fresh
  database, incremented once per successful whole-list replacement);
* every registered graph: its original ``nodes``/``export`` submission and the
  review result produced under the registry generation the graph was last
  reviewed at;
* every sealed export package: the graph it was sealed from, the sample
  ``columns``/``rows`` frozen at seal time, the registry ``generation`` the
  package was created under, and — once released — the ``release_generation``
  the package was released under (NULL while unreleased). A package purged
  by source column keeps its id but loses its ``rows`` and release record
  and carries a persistent ``purged`` marker.

All reads and writes of the in-process state go through one lock, and a
replacement persists the new entries, the new generation and every graph's new
review result in a single explicit SQLite transaction: either the whole change
commits or nothing at all changes, so a registry can never be observed
together with stale conclusions — not even after a write or commit failure,
and not after the database is reopened. Sealing a package and recording its
release are likewise single atomic writes: a failed write or commit never
leaves a new package or a new release record behind.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

# Default database location inside the container; docker-compose mounts a named
# volume there. Local runs create the file next to the working directory.
DEFAULT_DB_PATH = os.environ.get("RISK_API_DB", "data/export-risk.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensitive_entries (
    source_id TEXT NOT NULL,
    column_name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (source_id, column_name)
);
CREATE TABLE IF NOT EXISTS graphs (
    id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    nodes TEXT NOT NULL,
    export TEXT NOT NULL,
    review TEXT NOT NULL,
    review_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    graph_id TEXT NOT NULL,
    columns TEXT NOT NULL,
    rows TEXT NOT NULL,
    generation INTEGER NOT NULL,
    release_generation INTEGER,
    purged INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    """A single SQLite database guarded by one process-wide lock."""

    def __init__(self, path: Optional[str] = DEFAULT_DB_PATH):
        self._path = path or ":memory:"
        self.lock = threading.RLock()
        if self._path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()
        self._init_meta()

    def _migrate(self) -> None:
        """Bring a database file written by an older release up to date.

        Columns added after the first schema version are attached with
        ``ALTER TABLE`` when missing, so an existing database (the Compose
        named volume survives image updates) keeps working unchanged.
        """
        package_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(packages)")
        }
        if "purged" not in package_columns:
            self._conn.execute(
                "ALTER TABLE packages ADD COLUMN purged "
                "INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    # -------------------------------------------------------- transactions --

    @contextmanager
    def _transaction(self):
        """Run a batch of writes as one atomic unit.

        The connection runs in autocommit mode (``isolation_level=None``), so
        without an explicit ``BEGIN`` every statement would commit on its own
        and a failure halfway through would leave a partial state behind
        (e.g. new entries next to the old generation). Here the whole batch
        commits together; any failure — including a failed ``COMMIT`` — rolls
        everything back to the exact pre-batch state.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        try:
            self._conn.commit()
        except BaseException:
            # A failed commit can leave the transaction open; make sure the
            # partial work is discarded rather than lingering uncommitted.
            self._conn.rollback()
            raise

    # --------------------------------------------------------------- meta --

    def _init_meta(self) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES "
                "('generation', '0')"
            )

    def get_generation(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'generation'"
        ).fetchone()
        return int(row["value"])

    def set_generation(self, generation: int) -> None:
        self._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'generation'",
            (str(generation),),
        )

    # ---------------------------------------------------------- registry --

    def get_entries(self) -> list[tuple[str, str]]:
        """Return the registry entries in submission (ordinal) order."""
        rows = self._conn.execute(
            "SELECT source_id, column_name FROM sensitive_entries "
            "ORDER BY ordinal"
        ).fetchall()
        return [(row["source_id"], row["column_name"]) for row in rows]

    def replace_entries(
        self,
        entries: list[tuple[str, str]],
        generation: int,
        graph_reviews: dict[str, dict[str, object]],
    ) -> None:
        """Persist entries + generation + every graph's review atomically.

        ``graph_reviews`` maps every registered graph id to its review under
        the *new* registry. The caller has already computed the reviews, so
        the transaction itself never runs review logic. Any write or commit
        failure rolls the whole replacement back: entries, generation and
        every stored conclusion stay exactly as they were — including after
        the database is closed and reopened.
        """
        with self._transaction():
            self._conn.execute("DELETE FROM sensitive_entries")
            self._conn.executemany(
                "INSERT INTO sensitive_entries "
                "(source_id, column_name, ordinal) VALUES (?, ?, ?)",
                [
                    (source_id, column_name, ordinal)
                    for ordinal, (source_id, column_name) in enumerate(entries)
                ],
            )
            self.set_generation(generation)
            for graph_id, review in graph_reviews.items():
                self._conn.execute(
                    "UPDATE graphs SET review = ?, review_generation = ? "
                    "WHERE id = ?",
                    (json.dumps(review, ensure_ascii=False), generation, graph_id),
                )

    # ------------------------------------------------------------- graphs --

    def list_graph_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, nodes, export, review, review_generation "
            "FROM graphs ORDER BY ordinal"
        ).fetchall()

    def get_graph_row(self, graph_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, nodes, export, review, review_generation "
            "FROM graphs WHERE id = ?",
            (graph_id,),
        ).fetchone()

    def insert_graph(
        self,
        graph_id: str,
        nodes: list[dict[str, object]],
        export: str,
        review: dict[str, object],
        generation: int,
    ) -> None:
        next_ordinal = self._conn.execute(
            "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM graphs"
        ).fetchone()[0]
        with self._conn:
            self._conn.execute(
                "INSERT INTO graphs "
                "(id, ordinal, nodes, export, review, review_generation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    graph_id,
                    next_ordinal,
                    json.dumps(nodes, ensure_ascii=False),
                    export,
                    json.dumps(review, ensure_ascii=False),
                    generation,
                ),
            )

    # ----------------------------------------------------------- packages --

    def list_package_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, graph_id, columns, rows, generation, "
            "release_generation, purged FROM packages ORDER BY ordinal"
        ).fetchall()

    def get_package_row(self, package_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, graph_id, columns, rows, generation, "
            "release_generation, purged FROM packages WHERE id = ?",
            (package_id,),
        ).fetchone()

    def insert_package(
        self,
        package_id: str,
        graph_id: str,
        columns: list[str],
        rows: list[list[object]],
        generation: int,
    ) -> None:
        """Persist a freshly sealed (still unreleased) package atomically.

        A failed write or commit rolls the insert back: no half-written
        package is ever left behind.
        """
        next_ordinal = self._conn.execute(
            "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM packages"
        ).fetchone()[0]
        with self._conn:
            self._conn.execute(
                "INSERT INTO packages "
                "(id, ordinal, graph_id, columns, rows, generation, "
                " release_generation) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    package_id,
                    next_ordinal,
                    graph_id,
                    json.dumps(columns, ensure_ascii=False),
                    json.dumps(rows, ensure_ascii=False),
                    generation,
                ),
            )

    def release_package(self, package_id: str, release_generation: int) -> None:
        """Record the release generation atomically.

        Re-releasing after a registry change overwrites the previous release
        generation. A failed write or commit leaves the old release state
        (released or not) exactly as it was.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE packages SET release_generation = ? WHERE id = ?",
                (release_generation, package_id),
            )

    def purge_packages(self, package_ids: list[str]) -> None:
        """Mark packages purged, dropping their rows and release records.

        Every listed package is updated in one explicit transaction: the
        sealed ``rows`` are discarded, the release record is cleared and the
        persistent ``purged`` marker is set. A failed write or commit rolls
        the whole batch back — no package is left half-purged. The graph,
        the registry and the generation are never touched, and no attempt is
        made to scrub SQLite free pages or WAL remnants.
        """
        with self._transaction():
            self._conn.executemany(
                "UPDATE packages SET rows = '[]', release_generation = NULL, "
                "purged = 1 WHERE id = ?",
                [(package_id,) for package_id in package_ids],
            )


# The shared process-wide store, created lazily so importing the app never
# touches disk (tests and scripts stay free to rebind ``store`` first).
store: Optional[Store] = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global store
    if store is None:
        with _store_lock:
            if store is None:
                store = Store()
    return store


def reset_store(path: Optional[str] = DEFAULT_DB_PATH) -> Store:
    """Replace the process-wide store (tests use a throwaway database)."""
    global store
    with _store_lock:
        if store is not None:
            store.close()
        store = Store(path)
    return store
