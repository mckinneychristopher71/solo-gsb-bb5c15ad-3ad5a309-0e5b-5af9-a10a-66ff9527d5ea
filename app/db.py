"""SQLite storage for immutable, versioned flag configurations.

Each published configuration is stored as a single JSON document keyed by a
client-provided version. Rows are never updated or deleted, so re-publishing a
new version cannot change the result of any evaluation pinned to an old
version.

Environments (``production``, ``staging``, ...) hold a *pointer* to one of the
immutable versions. The pointer carries a monotonically increasing
``revision`` used for optimistic concurrency control: a pointer may only be
created with ``expected_revision == 0`` and only be moved by a caller that
presents the revision it last observed. Every check and the write happen in a
single ``BEGIN IMMEDIATE`` transaction, so a concurrent update loses the race
with ``409 stale_revision`` instead of silently overwriting the pointer.

Every successful pointer write also appends one row to ``environment_history``
inside the same transaction: environment name, the new revision, the target
version, the operation (``create``/``update``) and a timestamp. The row is
insert-only — nothing ever updates or deletes it — so the table is a
gap-free, immutable audit trail of how each pointer moved. Failed or stale
writes raise before the insert and roll back, leaving no record behind.

Change approvals (``change_approvals`` + ``approval_votes``) add a
review-then-execute flow on top of the pointer: an applicant opens a request
against the revision they observed (``base_revision``), distinct approvers
vote (each at most once, never the applicant), and once the recorded
``required_approvals`` threshold (1–5) is met the request may be executed.
Execution re-validates the environment revision inside one ``BEGIN
IMMEDIATE`` transaction: unchanged — the pointer moves and the immutable
history is appended in the same transaction; changed — the request is marked
``stale`` and the pointer stays put.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = os.environ.get("FLAG_DB_PATH", "data/flags.db")

_create_lock = threading.Lock()


class StaleRevision(Exception):
    """Raised when an environment write uses an outdated ``expected_revision``.

    This covers both the "create only with revision 0" rule (environment
    already exists but ``expected_revision == 0``) and lost-update detection
    (the observed revision no longer matches the current one). No row is
    modified when this is raised.
    """

    def __init__(self, name: str, expected: int, current: Optional[int]):
        self.name = name
        self.expected = expected
        self.current = current
        if current is None:
            message = f"environment {name!r} already exists; create requires expected_revision=0"
        else:
            message = (
                f"expected_revision {expected} is stale for environment {name!r};"
                f" current revision is {current}"
            )
        super().__init__(message)


class VersionNotFound(Exception):
    """Raised when a pointer targets a config version that was never published."""

    def __init__(self, version: str):
        self.version = version
        super().__init__(f"config version {version!r} not found")


class EnvironmentNotFound(Exception):
    """Raised when an operation targets an environment that does not exist."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"environment {name!r} not found")


class NoChange(Exception):
    """Raised when an approval targets the version the pointer already names."""

    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        super().__init__(
            f"environment {name!r} already points to version {version!r}"
        )


class ApprovalNotFound(Exception):
    """Raised when a change-approval id does not exist."""

    def __init__(self, approval_id: int):
        self.approval_id = approval_id
        super().__init__(f"change approval {approval_id} not found")


class ApprovalNotPending(Exception):
    """Raised when voting on / executing a request that is no longer pending."""

    def __init__(self, approval_id: int, status: str):
        self.approval_id = approval_id
        self.status = status
        super().__init__(
            f"change approval {approval_id} is {status!r}; only 'pending'"
            " requests accept votes or execution"
        )


class SelfApproval(Exception):
    """Raised when the applicant tries to vote on their own request."""

    def __init__(self, approval_id: int, approver: str):
        self.approval_id = approval_id
        self.approver = approver
        super().__init__(
            f"applicant {approver!r} may not approve their own request"
            f" {approval_id}"
        )


class DuplicateVote(Exception):
    """Raised when an approver votes twice on the same request."""

    def __init__(self, approval_id: int, approver: str):
        self.approval_id = approval_id
        self.approver = approver
        super().__init__(
            f"approver {approver!r} has already voted on change approval"
            f" {approval_id}"
        )


class ApprovalsNotMet(Exception):
    """Raised when executing before the recorded threshold of votes is met."""

    def __init__(self, approval_id: int, votes: int, required: int):
        self.approval_id = approval_id
        self.votes = votes
        self.required = required
        super().__init__(
            f"change approval {approval_id} has {votes} vote(s);"
            f" {required} required before execution"
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> str:
    return os.environ.get("FLAG_DB_PATH", DEFAULT_DB_PATH)


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection suitable for the request scope."""
    path = db_path or get_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    Path(parent).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_versions (
            version      TEXT PRIMARY KEY,
            payload      TEXT NOT NULL,
            flag_count   INTEGER NOT NULL,
            created_at   TEXT NOT NULL
        )
        """
    )
    # Environment pointers. ``revision`` starts at 1 on creation and is
    # incremented by every successful pointer update; it implements
    # optimistic concurrency control together with expected_revision.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environments (
            name         TEXT PRIMARY KEY,
            version      TEXT NOT NULL,
            revision     INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (version) REFERENCES config_versions(version)
        )
        """
    )
    # Immutable audit trail of pointer writes. One row per successful
    # create/update, written in the same transaction as the pointer move.
    # Deliberately no FOREIGN KEY: the audit log must survive even if the
    # pointer row were ever removed, and rows are never updated or deleted.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            environment  TEXT NOT NULL,
            revision     INTEGER NOT NULL,
            version      TEXT NOT NULL,
            operation    TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            UNIQUE (environment, revision)
        )
        """
    )
    # Change approvals: a review-then-execute request to move one
    # environment pointer to a target version. ``base_revision`` pins the
    # revision the applicant observed; execution re-validates it. ``status``
    # moves pending -> executed (pointer moved) or pending -> stale
    # (revision changed before execution). Rows are never deleted.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS change_approvals (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            environment        TEXT NOT NULL,
            target_version     TEXT NOT NULL,
            base_revision      INTEGER NOT NULL,
            applicant          TEXT NOT NULL,
            required_approvals INTEGER NOT NULL
                               CHECK (required_approvals BETWEEN 1 AND 5),
            status             TEXT NOT NULL
                               CHECK (status IN ('pending', 'executed', 'stale')),
            created_at         TEXT NOT NULL,
            executed_at        TEXT,
            executed_revision  INTEGER
        )
        """
    )
    # One row per (approval, approver). The composite PRIMARY KEY is the
    # final backstop for "each approver votes at most once", even if two
    # votes race past the in-transaction duplicate check.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_votes (
            approval_id  INTEGER NOT NULL REFERENCES change_approvals(id),
            approver     TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (approval_id, approver)
        )
        """
    )
    conn.commit()


def insert_version(
    conn: sqlite3.Connection, version: str, config: dict[str, Any]
) -> Optional[str]:
    """Persist a new immutable config version.

    Returns the created_at timestamp, or ``None`` if the version already
    exists. The INSERT is wrapped in an IMMEDIATE transaction so concurrent
    publishes of the same version cannot both succeed.
    """
    flags = config.get("flags", {})
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True)
    with _create_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM config_versions WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return None
            created_at = _utcnow()
            conn.execute(
                "INSERT INTO config_versions (version, payload, flag_count, created_at)"
                " VALUES (?, ?, ?, ?)",
                (version, payload, len(flags), created_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return created_at


def get_version(conn: sqlite3.Connection, version: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT version, payload, flag_count, created_at"
        " FROM config_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        return None
    return {
        "version": row["version"],
        "config": json.loads(row["payload"]),
        "flag_count": row["flag_count"],
        "created_at": row["created_at"],
    }


def list_versions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT version, flag_count, created_at"
        " FROM config_versions ORDER BY created_at ASC, version ASC"
    ).fetchall()
    return [
        {
            "version": row["version"],
            "flag_count": row["flag_count"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Environment pointers
# ---------------------------------------------------------------------------


def _env_row(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT name, version, revision, created_at, updated_at"
        " FROM environments WHERE name = ?",
        (name,),
    ).fetchone()


def upsert_environment(
    conn: sqlite3.Connection, name: str, version: str, expected_revision: int
) -> tuple[dict[str, Any], bool]:
    """Create or move the environment pointer with optimistic concurrency.

    The whole decision runs in one ``BEGIN IMMEDIATE`` transaction:

      * create: the environment must not exist and ``expected_revision`` must
        be 0; the new pointer starts at revision 1;
      * update: the stored revision must equal ``expected_revision`` and the
        target version must exist; the revision is incremented by one.

    Returns ``(environment, created)``. Raises :class:`StaleRevision` when the
    create/update precondition fails (the pointer is left untouched) and
    :class:`VersionNotFound` when the target version was never published.

    A successful write also appends one immutable row to
    ``environment_history`` inside the same transaction, so the audit trail
    and the pointer can never disagree. Failed writes raise before the
    history insert and roll back — no record is produced.
    """
    now = _utcnow()
    created = False
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = _env_row(conn, name)

        if existing is None:
            if expected_revision != 0:
                raise StaleRevision(name, expected_revision, None)
            # Confirm the referenced version exists before creating the row;
            # the FOREIGN KEY would also reject it, but we want a clean
            # version_not_found rather than a raw integrity error.
            if not _version_exists(conn, version):
                raise VersionNotFound(version)
            conn.execute(
                "INSERT INTO environments (name, version, revision, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?)",
                (name, version, now, now),
            )
            conn.execute(
                "INSERT INTO environment_history"
                " (environment, revision, version, operation, created_at)"
                " VALUES (?, 1, ?, 'create', ?)",
                (name, version, now),
            )
            created = True
        else:
            current_revision = existing["revision"]
            # Revision is checked first: a stale writer must not be able to
            # distinguish 404 from 409 or mutate anything.
            if current_revision != expected_revision:
                raise StaleRevision(name, expected_revision, current_revision)
            if not _version_exists(conn, version):
                raise VersionNotFound(version)
            conn.execute(
                "UPDATE environments SET version = ?, revision = revision + 1, updated_at = ?"
                " WHERE name = ? AND revision = ?",
                (version, now, name, current_revision),
            )
            conn.execute(
                "INSERT INTO environment_history"
                " (environment, revision, version, operation, created_at)"
                " VALUES (?, ?, ?, 'update', ?)",
                (name, current_revision + 1, version, now),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    row = _env_row(conn, name)
    env = {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return env, created


def get_environment(conn: sqlite3.Connection, name: str) -> Optional[dict[str, Any]]:
    row = _env_row(conn, name)
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_environment_history(
    conn: sqlite3.Connection,
    name: str,
    from_revision: int = 0,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Return the immutable audit trail of one environment, oldest first.

    Only rows with ``revision >= from_revision`` are returned and the result
    is capped at ``limit`` entries (``None`` = no cap), which supports simple
    forward pagination: fetch a page, then re-query with
    ``from_revision = last_revision + 1``. Because history rows are written
    only by successful pointer writes, the revisions of one environment
    always form the gap-free sequence 1..N.
    """
    sql = (
        "SELECT environment, revision, version, operation, created_at"
        " FROM environment_history"
        " WHERE environment = ? AND revision >= ?"
        " ORDER BY revision ASC"
    )
    params: list[Any] = [name, from_revision]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "environment": row["environment"],
            "revision": row["revision"],
            "version": row["version"],
            "operation": row["operation"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_environment_snapshot(
    conn: sqlite3.Connection, name: str
) -> Optional[dict[str, Any]]:
    """Return the pointer and its pinned config in one read.

    A single SELECT gives the evaluation handler a consistent pointer
    snapshot: the version payload returned here is the one the pointer named
    at read time, even if another transaction moves the pointer afterwards.
    """
    row = conn.execute(
        "SELECT e.name        AS name,"
        "       e.version     AS version,"
        "       e.revision    AS revision,"
        "       e.created_at  AS created_at,"
        "       e.updated_at  AS updated_at,"
        "       c.payload     AS payload"
        " FROM environments e"
        " JOIN config_versions c ON c.version = e.version"
        " WHERE e.name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "config": json.loads(row["payload"]),
    }


def _version_exists(conn: sqlite3.Connection, version: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM config_versions WHERE version = ?", (version,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Change approvals
# ---------------------------------------------------------------------------


def _approval_row(conn: sqlite3.Connection, approval_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, environment, target_version, base_revision, applicant,"
        "       required_approvals, status, created_at, executed_at,"
        "       executed_revision"
        " FROM change_approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()


def _approval_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    votes = conn.execute(
        "SELECT approver FROM approval_votes"
        " WHERE approval_id = ? ORDER BY rowid ASC",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "environment": row["environment"],
        "target_version": row["target_version"],
        "base_revision": row["base_revision"],
        "applicant": row["applicant"],
        "required_approvals": row["required_approvals"],
        "status": row["status"],
        "votes": len(votes),
        "approvers": [vote["approver"] for vote in votes],
        "created_at": row["created_at"],
        "executed_at": row["executed_at"],
        "executed_revision": row["executed_revision"],
    }


def create_change_approval(
    conn: sqlite3.Connection,
    environment: str,
    target_version: str,
    base_revision: int,
    applicant: str,
    required_approvals: int,
) -> dict[str, Any]:
    """Open a change-approval request against an environment pointer.

    Every precondition is checked inside one ``BEGIN IMMEDIATE``
    transaction: the environment must exist, ``base_revision`` must match
    the current revision (the request is based on what the applicant
    observed), the target version must be published and must differ from
    the version the pointer currently names. Returns the pending request.
    """
    now = _utcnow()
    try:
        conn.execute("BEGIN IMMEDIATE")
        env = _env_row(conn, environment)
        if env is None:
            raise EnvironmentNotFound(environment)
        # Revision is checked before the version lookups, mirroring
        # upsert_environment: a stale base must not be able to distinguish
        # 404 from 409.
        if env["revision"] != base_revision:
            raise StaleRevision(environment, base_revision, env["revision"])
        if not _version_exists(conn, target_version):
            raise VersionNotFound(target_version)
        if env["version"] == target_version:
            raise NoChange(environment, target_version)
        cursor = conn.execute(
            "INSERT INTO change_approvals"
            " (environment, target_version, base_revision, applicant,"
            "  required_approvals, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                environment,
                target_version,
                base_revision,
                applicant,
                required_approvals,
                now,
            ),
        )
        approval_id = cursor.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _approval_detail(conn, _approval_row(conn, approval_id))


def get_change_approval(
    conn: sqlite3.Connection, approval_id: int
) -> Optional[dict[str, Any]]:
    """Return one request with its current status and vote tally."""
    row = _approval_row(conn, approval_id)
    if row is None:
        return None
    return _approval_detail(conn, row)


def add_approval_vote(
    conn: sqlite3.Connection, approval_id: int, approver: str
) -> dict[str, Any]:
    """Record one approver's vote on a pending request.

    Each approver votes at most once per request and the applicant may
    never vote on their own request. The checks and the insert run in one
    ``BEGIN IMMEDIATE`` transaction; the ``PRIMARY KEY (approval_id,
    approver)`` backstops the duplicate check under concurrency. Returns
    the request with the updated tally.
    """
    now = _utcnow()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _approval_row(conn, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        if row["status"] != "pending":
            raise ApprovalNotPending(approval_id, row["status"])
        if approver == row["applicant"]:
            raise SelfApproval(approval_id, approver)
        existing = conn.execute(
            "SELECT 1 FROM approval_votes WHERE approval_id = ? AND approver = ?",
            (approval_id, approver),
        ).fetchone()
        if existing is not None:
            raise DuplicateVote(approval_id, approver)
        conn.execute(
            "INSERT INTO approval_votes (approval_id, approver, created_at)"
            " VALUES (?, ?, ?)",
            (approval_id, approver, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _approval_detail(conn, _approval_row(conn, approval_id))


def execute_change_approval(
    conn: sqlite3.Connection, approval_id: int
) -> dict[str, Any]:
    """Execute an approved request: move the pointer it was opened against.

    One ``BEGIN IMMEDIATE`` transaction re-reads the request, confirms the
    vote threshold is met and re-validates the environment revision against
    the recorded ``base_revision``:

      * unchanged — move the pointer (revision + 1), append the immutable
        ``environment_history`` record and mark the request ``executed``,
        all in the same transaction, so the audit trail and the pointer
        can never disagree;
      * changed — mark the request ``stale`` (this update *is* committed)
        and raise :class:`StaleRevision`; the pointer and its history stay
        untouched.
    """
    now = _utcnow()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _approval_row(conn, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        if row["status"] != "pending":
            raise ApprovalNotPending(approval_id, row["status"])
        votes = conn.execute(
            "SELECT COUNT(*) AS n FROM approval_votes WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()["n"]
        if votes < row["required_approvals"]:
            raise ApprovalsNotMet(approval_id, votes, row["required_approvals"])

        env = _env_row(conn, row["environment"])
        current_revision = env["revision"] if env is not None else None
        if current_revision != row["base_revision"]:
            # The pointer moved after the request was opened, so the
            # approval no longer describes reality. Persist the stale
            # marker and leave the pointer untouched.
            conn.execute(
                "UPDATE change_approvals SET status = 'stale' WHERE id = ?",
                (approval_id,),
            )
            conn.commit()
            raise StaleRevision(
                row["environment"], row["base_revision"], current_revision
            )

        new_revision = current_revision + 1
        conn.execute(
            "UPDATE environments SET version = ?, revision = revision + 1, updated_at = ?"
            " WHERE name = ? AND revision = ?",
            (row["target_version"], now, row["environment"], current_revision),
        )
        conn.execute(
            "INSERT INTO environment_history"
            " (environment, revision, version, operation, created_at)"
            " VALUES (?, ?, ?, 'update', ?)",
            (row["environment"], new_revision, row["target_version"], now),
        )
        conn.execute(
            "UPDATE change_approvals"
            " SET status = 'executed', executed_at = ?, executed_revision = ?"
            " WHERE id = ?",
            (now, new_revision, approval_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _approval_detail(conn, _approval_row(conn, approval_id))
