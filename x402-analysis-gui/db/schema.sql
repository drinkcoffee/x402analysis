-- user_settings: the set of email addresses authorised to log into
-- x402-analysis-gui, plus each one's role and theme preference.
--
-- `email` itself is never stored in plaintext here -- see gui/crypto.py.
-- Lookups go by `email_hash` (a keyed HMAC-SHA256 of the normalized email,
-- computed with a subkey derived from DB_ENCRYPTION_KEY via HKDF-SHA256);
-- `email_encrypted` (AES-256-GCM: base64(nonce || ciphertext+tag)) is only
-- decrypted for display/audit purposes, not on the normal login path.
--
-- Run this once against your Neon database: `python scripts/init_db.py`,
-- or paste it into the Neon console's SQL editor.
CREATE TABLE IF NOT EXISTS user_settings (
    id               SERIAL PRIMARY KEY,
    email_hash       TEXT NOT NULL UNIQUE,
    email_encrypted  TEXT NOT NULL,
    user_type        SMALLINT NOT NULL DEFAULT 2 CHECK (user_type IN (0, 1, 2)),   -- 0=Admin, 1=Advanced, 2=Standard
    light_mode       SMALLINT NOT NULL DEFAULT 0 CHECK (light_mode IN (0, 1, 2)),  -- 0=auto, 1=light, 2=dark
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
