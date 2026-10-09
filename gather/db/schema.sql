-- Schema for gather's local database, created by 00_init_db.py.
--
-- Copied from x402-analysis-api/db/schema.sql, without its api_keys table
-- (that's only for the deployed API's authentication). Comments that
-- referred to x402-analysis-api's scripts now refer to gather's.

-- --------------------------------------------------------------------------
-- x402 ecosystem data: facilitators, servers, their services, clients, and
-- the on-chain addresses associated with any of them.
-- --------------------------------------------------------------------------

-- uris: one URL and what's known about it (IP, geolocation, TLS certificate
-- subject) -- referenced by facilitator/server's api/doc/website columns
-- below, so the same fingerprinting data isn't duplicated per referencer.
CREATE TABLE IF NOT EXISTS uris (
    id        SERIAL PRIMARY KEY,
    url       TEXT NOT NULL UNIQUE,
    ip        TEXT,
    location  TEXT,
    subject   TEXT,  -- TLS certificate subject name
    risk      SMALLINT NOT NULL DEFAULT 0,
    notes     TEXT,
    updated   DATE NOT NULL DEFAULT CURRENT_DATE
);

-- facilitator: one x402 payment facilitator. `name` is unique, so
-- facilitators can be looked up by name. `api` is nullable -- not
-- every known facilitator has a recorded API URL yet (some are only known
-- via their x402scan.com page); `doc`/`website`/`x402scan` are likewise
-- optional.
CREATE TABLE IF NOT EXISTS facilitator (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    api       INTEGER REFERENCES uris(id),
    doc       INTEGER REFERENCES uris(id),
    website   INTEGER REFERENCES uris(id),
    x402scan  INTEGER REFERENCES uris(id),  -- the facilitator's x402scan.com page, if known
    risk      SMALLINT NOT NULL DEFAULT 0,
    active    BOOLEAN NOT NULL DEFAULT TRUE,
    notes     TEXT,
    updated   DATE NOT NULL DEFAULT CURRENT_DATE,
    CONSTRAINT facilitator_name_key UNIQUE (name)
);

-- server: one x402-gated server (same shape as facilitator -- see above).
CREATE TABLE IF NOT EXISTS server (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    api       INTEGER REFERENCES uris(id),
    doc       INTEGER REFERENCES uris(id),
    website   INTEGER REFERENCES uris(id),
    x402scan  INTEGER REFERENCES uris(id),  -- kept symmetric with facilitator; unused until a server loader exists
    risk      SMALLINT NOT NULL DEFAULT 0,
    active    BOOLEAN NOT NULL DEFAULT TRUE,
    notes     TEXT,
    updated   DATE NOT NULL DEFAULT CURRENT_DATE,
    CONSTRAINT server_name_key UNIQUE (name)
);

-- service: one endpoint offered by a server. `category` is a free-text
-- label from gatherlib/service_classifier.py's keyword rules (e.g. "search",
-- "llm", "finance") -- set by 02_services.py, not curated here.
-- (server, path) is unique, so a server can't have the same path recorded
-- twice. `active` is for a liveness probe (an unpaid request getting HTTP
-- 402 back, the correct response from a live x402-gated endpoint) --
-- 02_services.py records services as not active, since it doesn't probe
-- them. Probing a live service any more meaningfully (getting past the
-- 402) would require an actual payment.
CREATE TABLE IF NOT EXISTS service (
    id           SERIAL PRIMARY KEY,
    server       INTEGER NOT NULL REFERENCES server(id),
    path         TEXT NOT NULL,
    description  TEXT,
    price        TEXT,
    tags         TEXT,
    category     TEXT,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    risk         SMALLINT NOT NULL DEFAULT 0,
    notes        TEXT,
    updated      DATE NOT NULL DEFAULT CURRENT_DATE,
    CONSTRAINT service_server_path_key UNIQUE (server, path)
);

-- clients: has no identifying field of its own -- a client is identified
-- entirely via the address(es) linked to it in linkaddresses below.
CREATE TABLE IF NOT EXISTS clients (
    id       SERIAL PRIMARY KEY,
    risk     SMALLINT NOT NULL DEFAULT 0,
    notes    TEXT,
    updated  DATE NOT NULL DEFAULT CURRENT_DATE
);

-- addresses: one on-chain address, deduplicated -- linkaddresses (below)
-- points to a row here rather than each linker storing its own copy.
CREATE TABLE IF NOT EXISTS addresses (
    id       SERIAL PRIMARY KEY,
    address  TEXT NOT NULL UNIQUE,
    chains   TEXT,  -- comma-separated if the address is used on more than one chain
    source   TEXT,  -- where this address was observed, e.g. "x402scan", "Docs", "/supported" -- comma-separated if more than one
    risk     SMALLINT NOT NULL DEFAULT 0,
    notes    TEXT,
    updated  DATE NOT NULL DEFAULT CURRENT_DATE
);

-- linkaddresses: associates one address with one facilitator/server/
-- client/funder/associate. `ref` is a polymorphic id -- which table it
-- refers to depends on `owner` -- so, unlike every other reference in this
-- schema, it can't carry a real foreign-key constraint (Postgres FKs can
-- only target one fixed table). owner=3 (funder) and owner=4 (associate)
-- don't have a table yet; only facilitator/server/clients (0/1/2) do.
-- (owner, ref, address) is unique so a loader can upsert (ON CONFLICT DO
-- NOTHING) instead of duplicating a link on every re-run.
CREATE TABLE IF NOT EXISTS linkaddresses (
    id       SERIAL PRIMARY KEY,
    owner    SMALLINT NOT NULL CHECK (owner IN (0, 1, 2, 3, 4)),  -- 0=facilitator, 1=server, 2=client, 3=funder, 4=associate
    ref      INTEGER NOT NULL,  -- id in the table named by `owner`; see note above
    address  INTEGER NOT NULL REFERENCES addresses(id),
    CONSTRAINT linkaddresses_owner_ref_address_key UNIQUE (owner, ref, address)
);

-- client_transactions: one USDC transfer from a client address to a
-- server's payment address, on Base (found by 05_find_clients_base.py).
-- `client` is the sending address; `server` is a server that address is
-- linked to -- a payment address shared by several servers gets one row
-- per server, so summing across servers counts those payments more than
-- once. `amount` is in whole units of `currency` (e.g. "0.05" USDC), kept
-- as text so no precision is lost. (tx_hash, log_index) identifies the
-- transfer on chain.
CREATE TABLE IF NOT EXISTS client_transactions (
    id         SERIAL PRIMARY KEY,
    client     INTEGER NOT NULL REFERENCES addresses(id),
    server     INTEGER NOT NULL REFERENCES server(id),
    amount     TEXT NOT NULL,
    currency   TEXT NOT NULL,
    tx_hash    TEXT NOT NULL,
    log_index  INTEGER NOT NULL,
    timestamp  TIMESTAMPTZ,
    CONSTRAINT client_transactions_transfer_server_key UNIQUE (tx_hash, log_index, server)
);
