"""Ground-truth isolation, asserted against a live database.

The fault injector knows the true root cause of every incident. That is the project's whole
asset — and it is worthless if the investigation engine can reach it. Enforcing the
separation by convention is not enough: if a reviewer suspects leakage, every accuracy
number we report is void, and "we were careful" is not a defence anyone should accept.

So the engine's database role holds no grant on the ground-truth database, and this test
asserts the connection itself fails. It runs in CI against a real Postgres, and skips
locally when none is available.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

AGENT_DSN = os.environ.get(
    "SHERLOG_AGENT_DSN", "postgresql://agent:agent@localhost:5432/sherlog_truth"
)
INJECTOR_DSN = os.environ.get(
    "SHERLOG_INJECTOR_DSN", "postgresql://injector:injector@localhost:5432/sherlog_truth"
)
AGENT_APP_DSN = os.environ.get(
    "SHERLOG_AGENT_APP_DSN", "postgresql://agent:agent@localhost:5432/nimbus"
)


def _database_available() -> bool:
    try:
        with psycopg.connect(INJECTOR_DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_available(),
    reason="no Postgres with the isolation schema applied; runs in CI",
)


def test_the_agent_role_cannot_connect_to_the_ground_truth_database():
    """The strongest available form of the guarantee.

    Not "cannot SELECT" — cannot connect at all. A separate database rather than a separate
    schema, because schema-level separation inside one database leaves a single misplaced
    GRANT between the engine and the answers.
    """
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(AGENT_DSN, connect_timeout=3)


def test_the_injector_role_can_write_ground_truth():
    """The other half: isolation that also blocked the injector would be useless."""
    with psycopg.connect(INJECTOR_DSN, connect_timeout=3) as conn:
        count = conn.execute("SELECT count(*) FROM injections").fetchone()
        assert count is not None


def test_the_agent_role_can_still_read_application_telemetry():
    """The engine must reach the deployments table — git_diff_since() depends on it — while
    remaining unable to reach the answers. Over-tight permissions would break the product;
    loose ones would break the science."""
    with psycopg.connect(AGENT_APP_DSN, connect_timeout=3) as conn:
        conn.execute("SELECT commit_sha, service, deployed_at FROM deployments LIMIT 1")


def test_no_ground_truth_table_is_reachable_from_the_application_database():
    """Guards against the obvious future mistake: someone adding an `injections` table to
    the app database "just for convenience"."""
    with psycopg.connect(AGENT_APP_DSN, connect_timeout=3) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert not names & {"injections", "scores", "ground_truth"}
