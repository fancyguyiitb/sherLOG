-- Application schema, the deployments table, and the jobs queue.
--
-- The deployments table is what makes git correlation possible at all: git_diff_since()
-- joins rows here to `git show` output. Without it the agent would have to guess which
-- commit was live at a given moment, and F-05 would be unsolvable rather than hard.

CREATE TABLE orders (
    id            UUID PRIMARY KEY,
    customer_id   TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'PENDING',  -- PENDING | COMPLETE | FAILED
    total_cents   INTEGER     NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX orders_status_created_idx ON orders (status, created_at);

CREATE TABLE order_items (
    id          BIGSERIAL PRIMARY KEY,
    order_id    UUID      NOT NULL REFERENCES orders (id),
    sku         TEXT      NOT NULL,
    quantity    INTEGER   NOT NULL DEFAULT 1,
    price_cents INTEGER
);

CREATE INDEX order_items_order_idx ON order_items (order_id);

CREATE TABLE catalog (
    sku         TEXT PRIMARY KEY,
    name        TEXT    NOT NULL,
    -- Nullable on purpose. F-06's crafted row has a NULL price here, and the worker's
    -- handler raises on it. A real defect reachable from real data beats a fault flag.
    price_cents INTEGER,
    -- Rich product content (~60 KB per SKU). Cached in Redis alongside the price, so the
    -- cache's working set (~12 MB) fits comfortably in 256 MB and not at all in F-02's 4 MB.
    description TEXT
);

-- Observed prices. An item's selling price is the 30-day median of these — an aggregate
-- over thousands of rows per SKU, which is exactly why orders-svc caches it.
CREATE TABLE price_history (
    sku         TEXT        NOT NULL,
    price_cents INTEGER     NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE inventory (
    sku      TEXT PRIMARY KEY REFERENCES catalog (sku),
    quantity INTEGER NOT NULL DEFAULT 1000
);

CREATE TABLE payments (
    id              TEXT PRIMARY KEY,
    order_id        UUID        NOT NULL REFERENCES orders (id),
    idempotency_key TEXT        NOT NULL UNIQUE,
    status          TEXT        NOT NULL,
    amount_cents    INTEGER     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Postgres-as-a-queue with FOR UPDATE SKIP LOCKED. Kafka would be a second system to run,
-- operate and explain, for a workload of 10 jobs/second.
CREATE TABLE jobs (
    id           BIGSERIAL PRIMARY KEY,
    queue        TEXT        NOT NULL DEFAULT 'fulfillment',
    payload      JSONB       NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'PENDING',  -- PENDING | RUNNING | DONE | DEAD
    attempts     INTEGER     NOT NULL DEFAULT 0,
    enqueued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error   TEXT
);

CREATE INDEX jobs_claim_idx ON jobs (queue, status, id);

-- Written by the injector on every git_commit-mode fault AND by the benign-deploy decoy.
-- The agent cannot tell them apart from this table alone, which is the point of D-2.
CREATE TABLE deployments (
    id           BIGSERIAL PRIMARY KEY,
    commit_sha   TEXT        NOT NULL,
    service      TEXT        NOT NULL,
    deployed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    diff_summary TEXT,
    author       TEXT
);

CREATE INDEX deployments_time_idx ON deployments (deployed_at DESC);
