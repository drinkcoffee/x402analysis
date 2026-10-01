-- Idempotent ALTER statements that bring an already-created (older-shape)
-- database up to date with db/schema.sql.
--
-- `CREATE TABLE IF NOT EXISTS` (in schema.sql) only helps for tables that
-- don't exist at all yet -- it can't add a column or constraint to a table
-- that was already created before that column/constraint was added to
-- schema.sql. This file covers that gap. Run by scripts/init_db.py right
-- after schema.sql, so one command (`python scripts/init_db.py`) handles
-- both a brand-new database and patching up an existing one.
--
-- Every statement here is safe to run any number of times, including
-- against a database that's already fully up to date (a complete no-op
-- then) -- `ADD COLUMN IF NOT EXISTS` and `DROP NOT NULL` are naturally
-- idempotent; the UNIQUE constraints use explicit names (matching the ones
-- schema.sql gives them on a fresh install) and a DO block that swallows
-- "already exists" so adding one twice doesn't error.

-- addresses.source: added after some databases already had `addresses`.
ALTER TABLE addresses ADD COLUMN IF NOT EXISTS source TEXT;

-- facilitator/server: the `x402scan` column (a "new type of URL" alongside
-- api/doc/website), and `api` made nullable -- not every known
-- facilitator/server has a recorded API URL yet.
ALTER TABLE facilitator ADD COLUMN IF NOT EXISTS x402scan INTEGER REFERENCES uris(id);
ALTER TABLE server      ADD COLUMN IF NOT EXISTS x402scan INTEGER REFERENCES uris(id);
ALTER TABLE facilitator ALTER COLUMN api DROP NOT NULL;
ALTER TABLE server      ALTER COLUMN api DROP NOT NULL;

-- facilitator.name / server.name: made UNIQUE so scripts/load_facilitators.py
-- can upsert by name instead of duplicating a row on every re-run.
DO $$ BEGIN
    ALTER TABLE facilitator ADD CONSTRAINT facilitator_name_key UNIQUE (name);
EXCEPTION
    WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE server ADD CONSTRAINT server_name_key UNIQUE (name);
EXCEPTION
    WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

-- linkaddresses: (owner, ref, address) made UNIQUE, so a loader can upsert
-- (ON CONFLICT DO NOTHING) instead of duplicating a link on every re-run.
DO $$ BEGIN
    ALTER TABLE linkaddresses ADD CONSTRAINT linkaddresses_owner_ref_address_key UNIQUE (owner, ref, address);
EXCEPTION
    WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

-- service.category: added after some databases already had `service`
-- (scripts/update.py's server/service scan needs it, even though
-- schema.sql already includes it for a brand-new install).
ALTER TABLE service ADD COLUMN IF NOT EXISTS category TEXT;

-- service.active: added for scripts/update.py's liveness probe of newly
-- discovered services (see schema.sql's comment on the service table).
ALTER TABLE service ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

-- service: (server, path) made UNIQUE, so scripts/update.py can tell a
-- service it already knows about from a new one instead of duplicating a
-- row on every re-run.
DO $$ BEGIN
    ALTER TABLE service ADD CONSTRAINT service_server_path_key UNIQUE (server, path);
EXCEPTION
    WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;
