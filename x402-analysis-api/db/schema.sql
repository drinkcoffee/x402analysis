-- api_keys: the set of API keys authorised to call x402-analysis-api's
-- protected endpoints (everything except GET / and GET /status).
--
-- Keys are never stored in plaintext -- only a SHA-256 hash of the raw key
-- (see lib/auth.py). A key is a high-entropy random token, not a guessable
-- password, so an unsalted hash is sufficient here; there is nothing to
-- decrypt or display later, since a lost key can only be replaced, never
-- recovered -- see scripts/create_api_key.py.
--
-- Run this once against your Neon database: `python scripts/init_db.py`,
-- or paste it into the Neon console's SQL editor.
CREATE TABLE IF NOT EXISTS api_keys (
    id            SERIAL PRIMARY KEY,
    key_hash      TEXT NOT NULL UNIQUE,
    access_level  TEXT NOT NULL CHECK (access_level IN ('read', 'read_write')),
    label         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
