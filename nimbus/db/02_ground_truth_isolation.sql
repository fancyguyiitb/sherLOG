-- Ground-truth isolation, enforced at the database-permission level.
--
-- The fault injector writes the ground-truth label for every injection. The investigation
-- engine must not be able to read it. Enforcing that by convention is not enough: if a
-- reviewer suspects leakage, every accuracy number in the writeup is void, and "we were
-- careful" is not an answer anyone should accept.
--
-- So: separate database, separate roles, zero grants. tests/test_ground_truth_isolation.py
-- asserts that role `agent` receives a permission error when it tries to read, and that
-- test runs in CI on every push.

CREATE ROLE injector WITH LOGIN PASSWORD 'injector';
CREATE ROLE agent    WITH LOGIN PASSWORD 'agent';

-- The engine reads application telemetry as `agent`, and nothing else.
GRANT CONNECT ON DATABASE nimbus TO agent;
GRANT USAGE  ON SCHEMA public TO agent;
GRANT SELECT  ON ALL TABLES IN SCHEMA public TO agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent;

-- A separate database, not merely a separate schema. Schema-level separation inside one
-- database leaves a single misplaced GRANT between the engine and the answers.
CREATE DATABASE sherlog_truth OWNER injector;

-- No grant of any kind to `agent`. The connection itself must fail.
REVOKE ALL ON DATABASE sherlog_truth FROM PUBLIC;
REVOKE CONNECT ON DATABASE sherlog_truth FROM PUBLIC;

\connect sherlog_truth

CREATE TABLE injections (
    id                     BIGSERIAL PRIMARY KEY,
    fault_id               TEXT        NOT NULL,      -- F-01 .. F-07
    injected_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at            TIMESTAMPTZ,
    decoys                 TEXT[]      NOT NULL DEFAULT '{}',
    -- The full ground_truth block from the fault YAML, frozen at injection time so a later
    -- catalog edit cannot retroactively change what a past run was scored against.
    ground_truth           JSONB       NOT NULL,
    catalog_version        TEXT        NOT NULL
);

CREATE TABLE scores (
    investigation_id       TEXT PRIMARY KEY,
    injection_id           BIGINT REFERENCES injections (id),
    rca_correct            BOOLEAN,
    proximate_correct      BOOLEAN,
    originating_correct    BOOLEAN,
    causal_chain_f1        REAL,
    decoy_blamed           BOOLEAN,
    abstained              BOOLEAN,
    abstention_correct     BOOLEAN,
    evidence_precision     REAL,
    hallucination_rate     REAL,
    claim_strip_rate       REAL,
    mttd_seconds           REAL,
    mttr_diagnosis_seconds REAL,
    cost_usd               REAL,
    degraded               BOOLEAN NOT NULL DEFAULT FALSE,
    judge_agreed           BOOLEAN,   -- NULL unless this run was in the 20% hand-scored sample
    scored_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT ALL ON ALL TABLES IN SCHEMA public TO injector;
