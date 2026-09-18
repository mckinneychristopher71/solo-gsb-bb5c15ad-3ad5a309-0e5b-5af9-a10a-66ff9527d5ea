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

Environment *change approvals* add a gated path for moving a pointer. An
approval records the environment, a target version, the pointer revision it
was opened against (``base_revision``), the applicant and how many distinct
approvers must vote (1–5). The applicant can never vote, each approver votes
at most once, and execution is only allowed once the vote count reaches the
threshold. Execution re-checks the pointer revision **inside the same
``BEGIN IMMEDIATE`` transaction** that performs the move (and its history
append): if the revision still equals ``base_revision`` the pointer moves;
if it changed meanwhile the approval is instead marked ``stale`` and the
pointer is left exactly where it was.
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
    """Raised when an approval references an environment that does not exist."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"environment {name!r} not found")


# ---------------------------------------------------------------------------
# Environment change approvals
# ---------------------------------------------------------------------------

APPROVAL_PENDING = "pending"
APPROVAL_EXECUTED = "executed"
APPROVAL_STALE = "stale"


class ApprovalNotFound(Exception):
    """Raised when an approval id does not exist."""

    def __init__(self, approval_id: int):
        self.approval_id = approval_id
        super().__init__(f"approval {approval_id} not found")


class TargetMatchesPointer(Exception):
    """Raised when an approval targets the version the pointer is already on."""

    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        super().__init__(
            f"target version {version!r} is already the current pointer"
            f" of environment {name!r}"
        )


class ApprovalNotPending(Exception):
    """Raised when voting on / executing an approval that is no longer pending."""

    def __init__(self, approval_id: int, status: str):
        self.approval_id = approval_id
        self.status = status
        super().__init__(f"approval {approval_id} is {status}, not pending")


class ApplicantCannotApprove(Exception):
    """Raised when the applicant tries to vote on their own approval."""

    def __init__(self, approval_id: int, applicant: str):
        self.approval_id = approval_id
        self.applicant = applicant
        super().__init__(
            f"applicant {applicant!r} cannot approve approval {approval_id}"
        )


class ApproverAlreadyVoted(Exception):
    """Raised when an approver votes on the same approval a second time."""

    def __init__(self, approval_id: int, approver: str):
        self.approval_id = approval_id
        self.approver = approver
        super().__init__(
            f"approver {approver!r} has already voted on approval {approval_id}"
        )


class InsufficientApprovals(Exception):
    """Raised when execution is attempted before the vote threshold is met."""

    def __init__(self, approval_id: int, votes: int, required: int):
        self.approval_id = approval_id
        self.votes = votes
        self.required = required
        super().__init__(
            f"approval {approval_id} has {votes} of {required} required approvals"
        )


class ApprovalStale(Exception):
    """Raised (after marking the approval stale) when execution finds the
    environment pointer has moved since the approval was opened.

    The approval row has already been flipped to ``stale`` in the same
    transaction; the pointer itself is never modified.
    """

    def __init__(self, approval_id: int, base_revision: int, current_revision: int):
        self.approval_id = approval_id
        self.base_revision = base_revision
        self.current_revision = current_revision
        super().__init__(
            f"approval {approval_id} is stale: environment revision was"
            f" {base_revision} when opened but is {current_revision} now"
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
    # Gated environment changes. ``base_revision`` snapshots the pointer
    # revision at creation time; execution only moves the pointer when the
    # revision still matches. An approval row is created in ``pending`` and
    # transitions exactly once, to ``executed`` (pointer moved) or ``stale``
    # (pointer had moved meanwhile; pointer untouched).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_approvals (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            environment        TEXT NOT NULL,
            target_version     TEXT NOT NULL,
            base_revision      INTEGER NOT NULL,
            applicant          TEXT NOT NULL,
            required_approvals INTEGER NOT NULL,
            status             TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            executed_at        TEXT
        )
        """
    )
    # One vote per distinct approver per approval; the applicant is rejected
    # by the application layer before any insert reaches this table.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_votes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL,
            approver    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (approval_id, approver),
            FOREIGN KEY (approval_id) REFERENCES environment_approvals(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_approvals_environment
            ON environment_approvals(environment)
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
# Environment change approvals
# ---------------------------------------------------------------------------


def _approval_row(
    conn: sqlite3.Connection, approval_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, environment, target_version, base_revision, applicant,"
        "       required_approvals, status, created_at, executed_at"
        " FROM environment_approvals WHERE id = ?",
        (approval_id,),
    ).fetchone()


def _list_votes(conn: sqlite3.Connection, approval_id: int) -> list[dict[str, Any]]:
    """Votes in cast order (insertion order = ascending vote id)."""
    rows = conn.execute(
        "SELECT approver, created_at FROM approval_votes"
        " WHERE approval_id = ? ORDER BY id ASC",
        (approval_id,),
    ).fetchall()
    return [
        {"approver": row["approver"], "voted_at": row["created_at"]} for row in rows
    ]


def create_approval(
    conn: sqlite3.Connection,
    environment: str,
    target_version: str,
    applicant: str,
    required_approvals: int,
) -> dict[str, Any]:
    """Open a pending change approval for an environment pointer.

    The environment must exist, the target version must be published, and it
    must differ from the version the pointer currently names. The pointer's
    current revision is snapshotted as ``base_revision`` for the later
    execution-time re-check. All checks run inside one ``BEGIN IMMEDIATE``
    transaction; failures roll back without creating a row.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = _env_row(conn, environment)
        if existing is None:
            raise EnvironmentNotFound(environment)
        # Unknown target version wins over the "same pointer" check: callers
        # must not learn "already there" for a version that was never
        # published.
        if not _version_exists(conn, target_version):
            raise VersionNotFound(target_version)
        if existing["version"] == target_version:
            raise TargetMatchesPointer(environment, target_version)

        now = _utcnow()
        cursor = conn.execute(
            "INSERT INTO environment_approvals"
            " (environment, target_version, base_revision, applicant,"
            "  required_approvals, status, created_at, executed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                environment,
                target_version,
                existing["revision"],
                applicant,
                required_approvals,
                APPROVAL_PENDING,
                now,
            ),
        )
        approval_id = cursor.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return get_approval(conn, approval_id)


def vote_approval(
    conn: sqlite3.Connection, approval_id: int, approver: str
) -> dict[str, Any]:
    """Cast one approval vote.

    The approval must still be pending, the approver must not be the
    applicant, and the same approver can vote only once (enforced both here
    and by ``UNIQUE(approval_id, approver)``). Returns the updated approval
    detail; reaching the threshold does not execute automatically — the
    caller invokes :func:`execute_approval`.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = _approval_row(conn, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        if row["status"] != APPROVAL_PENDING:
            raise ApprovalNotPending(approval_id, row["status"])
        if approver == row["applicant"]:
            raise ApplicantCannotApprove(approval_id, approver)

        already = conn.execute(
            "SELECT 1 FROM approval_votes WHERE approval_id = ? AND approver = ?",
            (approval_id, approver),
        ).fetchone()
        if already is not None:
            raise ApproverAlreadyVoted(approval_id, approver)

        conn.execute(
            "INSERT INTO approval_votes (approval_id, approver, created_at)"
            " VALUES (?, ?, ?)",
            (approval_id, approver, _utcnow()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return get_approval(conn, approval_id)


def execute_approval(conn: sqlite3.Connection, approval_id: int) -> dict[str, Any]:
    """Execute a threshold-met approval, re-checking the pointer in the same
    transaction.

    Inside one ``BEGIN IMMEDIATE`` transaction:

      * the approval must be ``pending`` and hold at least
        ``required_approvals`` distinct votes;
      * the environment pointer's revision is read again and compared with
        the ``base_revision`` snapshotted at creation:
          - unchanged → move the pointer to ``target_version``, increment the
            revision and append an ``update`` row to ``environment_history``
            (exactly as a direct pointer PUT would), all atomically;
          - changed (or the environment vanished) → flip the approval to
            ``stale`` and leave the pointer untouched.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = _approval_row(conn, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        if row["status"] != APPROVAL_PENDING:
            raise ApprovalNotPending(approval_id, row["status"])

        vote_count = conn.execute(
            "SELECT COUNT(*) AS n FROM approval_votes WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()["n"]
        if vote_count < row["required_approvals"]:
            raise InsufficientApprovals(
                approval_id, vote_count, row["required_approvals"]
            )

        now = _utcnow()
        env = _env_row(conn, row["environment"])
        # A different revision (including the environment no longer existing,
        # treated as revision 0) means someone else moved the pointer after
        # this approval was opened: mark the approval stale, do not write to
        # the pointer or the audit trail.
        if env is None or env["revision"] != row["base_revision"]:
            current_revision = env["revision"] if env is not None else 0
            conn.execute(
                "UPDATE environment_approvals SET status = ?, executed_at = ?"
                " WHERE id = ?",
                (APPROVAL_STALE, now, approval_id),
            )
            conn.commit()
            raise ApprovalStale(
                approval_id, row["base_revision"], current_revision
            )

        target = row["target_version"]
        new_revision = env["revision"] + 1
        # The target version's existence was guaranteed at creation time and
        # config versions are immutable/never deleted, so it still exists.
        conn.execute(
            "UPDATE environments SET version = ?, revision = revision + 1,"
            " updated_at = ? WHERE name = ? AND revision = ?",
            (target, now, env["name"], env["revision"]),
        )
        conn.execute(
            "INSERT INTO environment_history"
            " (environment, revision, version, operation, created_at)"
            " VALUES (?, ?, ?, 'update', ?)",
            (env["name"], new_revision, target, now),
        )
        conn.execute(
            "UPDATE environment_approvals SET status = ?, executed_at = ?"
            " WHERE id = ?",
            (APPROVAL_EXECUTED, now, approval_id),
        )
        conn.commit()
    except ApprovalStale:
        # The stale transition was already committed above; do not roll it
        # back in the generic handler.
        raise
    except Exception:
        conn.rollback()
        raise

    return get_approval(conn, approval_id)


def get_approval(
    conn: sqlite3.Connection, approval_id: int
) -> Optional[dict[str, Any]]:
    """Return an approval with its votes and current approval counts, or
    ``None`` when the id is unknown."""
    row = _approval_row(conn, approval_id)
    if row is None:
        return None
    votes = _list_votes(conn, approval_id)
    return {
        "id": row["id"],
        "environment": row["environment"],
        "target_version": row["target_version"],
        "base_revision": row["base_revision"],
        "applicant": row["applicant"],
        "required_approvals": row["required_approvals"],
        "status": row["status"],
        "created_at": row["created_at"],
        "executed_at": row["executed_at"],
        "votes": votes,
        "vote_count": len(votes),
        "remaining_approvals": max(
            0, row["required_approvals"] - len(votes)
        ),
    }
