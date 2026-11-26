-- Seed data. 200 SKUs match load/checkout.js.

INSERT INTO catalog (sku, name, price_cents, description)
SELECT 'SKU-' || lpad(i::text, 4, '0'),
       'Item ' || i,
       999 + (i * 37) % 9000,
       repeat(md5(i::text), 1900)            -- ~60 KB of product content
  FROM generate_series(0, 199) AS i;

INSERT INTO inventory (sku, quantity)
SELECT sku, 1000000 FROM catalog;

-- 10k observations per SKU, interleaved across the heap (as real append-only price feeds
-- are) and spread over 45 days. ~20ms per SKU to aggregate: cheap when cached, painful when not.
INSERT INTO price_history (sku, price_cents, observed_at)
SELECT 'SKU-' || lpad((g % 200)::text, 4, '0'),
       900 + ((g::bigint * 7919) % 9100)::int,
       now() - ((g % 64800) * interval '1 minute')
  FROM generate_series(0, 1999999) AS g;

CREATE INDEX price_history_sku_idx ON price_history (sku);
ANALYZE;
