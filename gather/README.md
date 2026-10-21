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
| `06_find_clients_solana.py` | The same for Solana: every account that has sent real USDC to a server's Solana address, through Helius's Solana RPC (needs `HELIUS_API_KEY`), another provider's with `--rpc-url` (needs `SOLANA_RPC_URL`), or Solana's public RPC with `--public-rpc`. Writes `dataset/clients_solana.json`. Takes hours. |
| `07_find_funders_base.py` | Finds funders on Base: who sent ETH to facilitator and server addresses, and USDC to client addresses, grouped by funding address. Writes `dataset/funders.json`. Doesn't change the database. Takes hours. Finds USDC by reading each client address's ERC-20 transfers (`--usdc-only-query` asks for just its USDC transfers instead, which is slow for some addresses). Reads at most 5 pages of each explorer query per address (`--max-pages N`); checks that stop with more pages left are listed under `incomplete`, and `--more-pages` carries on with them. |
| `08_find_funders_solana.py` | The same for Solana: who sent SOL to facilitator and server addresses, and USDC to client addresses (needs 06 to have finished). Uses Helius, another provider with `--rpc-url`, or Solana's public RPC with `--public-rpc`. Writes `dataset/funders_solana.json`. Doesn't change the database. Can take days: facilitators have millions of transactions. |
| `09_fetch_metadata_base.py` | Looks up the name, tags and warning notes Blockscout's explorer shows for every facilitator, server, client and funder address on Base (needs 07 to have finished; adds funders to `addresses`). Stores addresses that have any in `address_metadata`, and writes `dataset/metadata_base.json`. |
| `11_gather_base_erc8004.py` | Gathers every AI agent registered on Base under ERC-8004 (the ones 8004agents.ai shows): owner, verified agent wallet, and from each agent's registration file its name, description, `x402Support` and web, MCP and agentWallet endpoints. Reads Base's identity registry through Base's public RPC (or `BASE_RPC_URL`), stores agents in `erc8004_agents` as it goes, and writes `dataset/erc8004-agents-base.json`. Doesn't depend on the other scripts. Takes hours (about 130,000 agents). Re-running carries on where it stopped and picks up newly registered agents; `--retry-failed` tries the unreadable registration files again. |
| `20_gather_sanctions_lists.py` | Downloads the latest OFAC SDN list and Australia's consolidated sanctions list, and combines them, with any blockchain addresses they give, into `sanctions_entries` and `sanctions_addresses` and `dataset/sanctions_list.json`. Doesn't depend on the other scripts, and should be re-run to pick up list updates: each run replaces the previous one's data. |
| `30_countries_fac.py` | Counts the facilitators' API and documentation servers by the country their IP address geolocates to (from `dataset/facilitators.json`), and writes them as a LaTeX table to `dataset/facilitators-countries.tex`, sorted by API servers. Doesn't use the database; can be re-run. |
| `31_countries_servers.py` | The same for servers (from `dataset/services.json`): writes `dataset/servers-countries.tex`, with servers that have no documentation URL in a row of their own. Doesn't use the database; can be re-run. |
| `32_service_categories.py` | Counts the services in each category (from the hand-annotated `dataset/services-annotated.json`, names as annotated), writes them as a LaTeX table to `dataset/service-categories.tex` in alphabetical order (`*` categories first) with Uncategorised and Service inactive last, and prints the same table. Doesn't use the database; can be re-run. |
| `33_categorise_metadata_base.py` | Prints the unique names and the unique tags strings in `dataset/metadata_base.json` (tags not split on `;`), in two sections, alphabetically, each with the number of addresses that have it. Doesn't use the database; can be re-run. |
| `34_check_sanctions_base.py` | Checks every facilitator, server, client and funder address in `dataset/` (Base and Solana; funders from `funders.json` or its pieces) against `dataset/sanctions_list.json`, and prints each one on a list -- address, kinds, and what it's listed as -- or `None`. Doesn't use the database; re-run after 20 or after gathering more addresses. |
| `35_malicious_analysis.py` | Prints each address in `dataset/metadata_base.json` whose name or tags include "phish" (ignoring case), with which of facilitator, server, client and funder it is, and under each funder the addresses it funded (indented; a client already listed under an earlier funder is marked `duplicate`; a server also gets its `api_url` and service categories from `dataset/services-annotated.json`, a line per server) -- or `None`. `--only KIND ...` (facilitator, server, client, funder) shows only addresses of those kinds. Doesn't use the database; can be re-run. |
| `36_server_usdc_received.py` | Ranks server payment addresses (Base and Solana; `--chain` for one) by the USDC they received, from `client_transactions`, counting each transfer once even when the address is shared by several servers, with transfers, clients, the median transfer, how much came from phishing clients or clients phishing funders funded (as in 35) and what percentage that is, whether the address was funded by a phishing funder, and the servers linked to each with their countries and service categories (from `dataset/services-annotated.json`). Top 20 (`--top N`, 0 for all); every row is also written to `dataset/phishing_funds_flow.csv`. Reads the database; changes nothing there. |
| `40_split_funders_file.py` | `split` cuts `dataset/funders.json` (over GitHub's 100 MB limit, so it's in `.gitignore`) into `dataset/funders.part1.json` and `funders.part2.json`, each valid JSON and about 72 MB; `restore` rebuilds `funders.json` from them, byte-for-byte (checked by SHA-256). Restore won't overwrite a different `funders.json` without `--force`. Doesn't use the database. |

`01`, `02`, `05`, `06` and `09` won't run if their JSON file already exists or
their table already has rows (for `05`, `06` and `09`, rows for their chain;
`06` carries on if it has a progress file), and `07` and `08` won't run if
their JSON file exists, so nothing is gathered twice by accident. `06`, `07`
and `08` are resumable: they save their progress in `localdb/progress/` as they
go (`06` also commits each server address's clients and transactions to the
database as it goes), so if one stops part way, running it again carries on
from where it got to. Ethereum-style addresses are stored in lower case.

## Setup

Needs PostgreSQL installed (e.g. `brew install postgresql@18`); the scripts
run their own Postgres server, with its data in `localdb/` (gitignored), on
port 5434.

```sh
python3 -m venv gather/.venv
gather/.venv/bin/pip install -r gather/requirements.txt
cp gather/.env.example gather/.env   # then add your CDP keys (for 03) and Helius key or other Solana RPC URL (for 06 and 08)
```

## Running

```sh
gather/.venv/bin/python gather/00_init_db.py
gather/.venv/bin/python gather/01_facilitators.py
gather/.venv/bin/python gather/02_services.py
gather/.venv/bin/python gather/03_facilitator_liveness_check.py
gather/.venv/bin/python gather/04_service_liveness_check.py
gather/.venv/bin/python gather/05_find_clients_base.py
gather/.venv/bin/python gather/06_find_clients_solana.py   # or --rpc-url, or --public-rpc
gather/.venv/bin/python gather/07_find_funders_base.py
gather/.venv/bin/python gather/08_find_funders_solana.py   # or --rpc-url, or --public-rpc
gather/.venv/bin/python gather/09_fetch_metadata_base.py
gather/.venv/bin/python gather/11_gather_base_erc8004.py      # any time after 00; re-run to pick up new agents
gather/.venv/bin/python gather/20_gather_sanctions_lists.py   # any time after 00; re-run to refresh
gather/.venv/bin/python gather/30_countries_fac.py            # any time after 01; re-run after 01
gather/.venv/bin/python gather/31_countries_servers.py        # any time after 02; re-run after 02
gather/.venv/bin/python gather/32_service_categories.py       # re-run after editing dataset/services-annotated.json
gather/.venv/bin/python gather/33_categorise_metadata_base.py > names-and-tags.txt
gather/.venv/bin/python gather/34_check_sanctions_base.py       # after 20 and 07
gather/.venv/bin/python gather/35_malicious_analysis.py         # after 09 and 07; re-run after editing dataset/services-annotated.json
gather/.venv/bin/python gather/36_server_usdc_received.py      # after 05 and 06
gather/.venv/bin/python gather/40_split_funders_file.py split  # after 07, before committing; restore after a clone
```

To start again from scratch, drop the database (`dropdb -p 5434
x402_gather`) and delete the JSON files in `dataset/`.

To stop the Postgres server: `pg_ctl -D gather/localdb/pgdata stop`.
