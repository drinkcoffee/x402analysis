# gather

Scripts that gather data about the x402 ecosystem into a local Postgres
database, and into JSON files in `dataset/` (committed to git). They're
run in order, and don't depend on code anywhere else in this repo: what
they need from elsewhere is copied into `gatherlib/`, and each copied
module says where it came from.

| Script | What it does |
| --- | --- |
| `00_init_db.py` | Creates the database, with the tables in `db/schema.sql`. Fails if it already exists. |
| `01_facilitators.py` | Scrapes facilitators from x402scan.com, adds API and doc URLs from a built-in list, fingerprints their URLs, and stores them. Writes `dataset/facilitators.json`. |
| `02_services.py` | Gathers servers and their services from x402scan.com and Coinbase's CDP Bazaar, fingerprints their URLs, and stores them. Writes `dataset/services.json`. |
| `03_facilitator_liveness_check.py` | Checks each facilitator's `/supported` endpoint and sets whether it's active. Writes `dataset/facilitator_liveness.json`. Can be re-run. |
| `04_service_liveness_check.py` | Calls every service without paying; a service is live if it answers with an x402 payment request, and a server is live if any of its services is. Sets both in the database and writes `dataset/server_and_services_liveness.json`. Can be re-run. |
| `05_find_clients_base.py` | Finds clients: every account that has sent real USDC on Base to a server's address, from Blockscout's Base explorer. Adds them to `addresses`, `clients` and `client_transactions` (one row per transfer), and writes `dataset/clients.json`. Takes hours. |
| `06_find_clients_solana.py` | The same for Solana: every account that has sent real USDC to a server's Solana address, through Helius's Solana RPC (needs `HELIUS_API_KEY`). Writes `dataset/clients_solana.json`. Takes hours. |
| `07_find_funders_base.py` | Finds funders on Base: who sent ETH to facilitator and server addresses, and USDC to client addresses, grouped by funding address. Writes `dataset/funders.json`. Doesn't change the database. Takes hours. |
| `08_find_funders_solana.py` | The same for Solana: who sent SOL to facilitator and server addresses, and USDC to client addresses (needs 06 to have finished). Uses Helius, or Solana's public RPC with `--public-rpc`. Writes `dataset/funders_solana.json`. Doesn't change the database. Can take days: facilitators have millions of transactions. |
| `09_fetch_metadata_base.py` | Looks up the name, tags and warning notes Blockscout's explorer shows for every facilitator, server, client and funder address on Base (needs 07 to have finished; adds funders to `addresses`). Stores addresses that have any in `address_metadata`, and writes `dataset/metadata_base.json`. |

`01`, `02`, `05`, `06` and `09` won't run if their JSON file already exists or
their table already has rows (for `05`, `06` and `09`, rows for their chain),
and `07` and `08` won't run if their JSON file exists, so nothing is gathered
twice by accident. `07` and `08` are resumable: they save their progress in
`localdb/progress/` as they go, so if one stops part way, running it again
carries on from where it got to. Ethereum-style addresses are stored in lower
case.

## Setup

Needs PostgreSQL installed (e.g. `brew install postgresql@18`); the scripts
run their own Postgres server, with its data in `localdb/` (gitignored), on
port 5434.

```sh
python3 -m venv gather/.venv
gather/.venv/bin/pip install -r gather/requirements.txt
cp gather/.env.example gather/.env   # then add your CDP keys (for 03) and Helius key (for 06 and 08)
```

## Running

```sh
gather/.venv/bin/python gather/00_init_db.py
gather/.venv/bin/python gather/01_facilitators.py
gather/.venv/bin/python gather/02_services.py
gather/.venv/bin/python gather/03_facilitator_liveness_check.py
gather/.venv/bin/python gather/04_service_liveness_check.py
gather/.venv/bin/python gather/05_find_clients_base.py
gather/.venv/bin/python gather/06_find_clients_solana.py
gather/.venv/bin/python gather/07_find_funders_base.py
gather/.venv/bin/python gather/08_find_funders_solana.py   # or --public-rpc
gather/.venv/bin/python gather/09_fetch_metadata_base.py
```

To start again from scratch, drop the database (`dropdb -p 5434
x402_gather`) and delete the JSON files in `dataset/`.

To stop the Postgres server: `pg_ctl -D gather/localdb/pgdata stop`.
