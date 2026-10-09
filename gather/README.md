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

`01`, `02` and `05` won't run if their JSON file already exists or their table
already has rows, so nothing is gathered twice by accident. Ethereum-style
addresses are stored in lower case.

## Setup

Needs PostgreSQL installed (e.g. `brew install postgresql@18`); the scripts
run their own Postgres server, with its data in `localdb/` (gitignored), on
port 5434.

```sh
python3 -m venv gather/.venv
gather/.venv/bin/pip install -r gather/requirements.txt
cp gather/.env.example gather/.env   # then add your CDP keys, for 03
```

## Running

```sh
gather/.venv/bin/python gather/00_init_db.py
gather/.venv/bin/python gather/01_facilitators.py
gather/.venv/bin/python gather/02_services.py
gather/.venv/bin/python gather/03_facilitator_liveness_check.py
gather/.venv/bin/python gather/04_service_liveness_check.py
gather/.venv/bin/python gather/05_find_clients_base.py
```

To start again from scratch, drop the database (`dropdb -p 5434
x402_gather`) and delete the JSON files in `dataset/`.

To stop the Postgres server: `pg_ctl -D gather/localdb/pgdata stop`.
