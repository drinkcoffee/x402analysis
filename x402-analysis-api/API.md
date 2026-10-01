# x402 Analysis API

Base URL: `https://<your-deployment>.vercel.app`

Auth: send an API key in the `X-API-Key` header. Two access levels:
`read` (read-only endpoints) and `read_write` (read-only and read-write
endpoints; a `read_write` key also satisfies a `read` requirement).
`GET /` and `GET /status` need no key.

## GET /

- auth: none
- response: `text/html` -- a minimal page whose body is one line pointing
  to this file (HTML rather than plain text so the browser tab favicon
  actually shows up; see `<link rel="icon">` in the page).

## GET /status

- auth: none
- response: `application/json`

```json
{ "server": "online", "database": "online" }
```

| field | values | meaning |
| --- | --- | --- |
| `server` | `"online"` | always this if the server responds at all |
| `database` | `"online"` \| `"offline"` | whether Neon was reachable just now |

HTTP status: `200` if `database` is `"online"`, `503` if `"offline"`.

## GET /facilitators

- auth: `X-API-Key`, `read` or `read_write`
- response: `application/json` -- an array, ordered by name

```json
[
  { "name": "Coinbase", "risk": 0, "active": true },
  { "name": "Dexter", "risk": 0, "active": true }
]
```

## GET /facilitators/{name}

- auth: `X-API-Key`, `read` or `read_write`
- `{name}` is matched against `facilitator.name` case-insensitively
- response: `application/json` -- every column on the facilitator row, its
  `api`/`doc`/`website`/`x402scan` URLs each expanded to the full `uris` row
  (not just the bare URL -- IP, geolocation, TLS certificate subject are
  "information available" about it too; `null` if that URL type isn't
  known), and every address linked to it

```json
{
  "id": 5,
  "name": "Coinbase",
  "risk": 0,
  "active": true,
  "notes": null,
  "updated": "2026-09-28",
  "api": {
    "url": "https://api.cdp.coinbase.com/platform",
    "ip": "172.64.152.241",
    "location": "Toronto, Ontario, Canada",
    "subject": "coinbase.com",
    "risk": 0,
    "notes": null,
    "updated": "2026-09-28"
  },
  "doc": { "...": "same shape as api, or null" },
  "website": null,
  "x402scan": { "...": "same shape as api, or null" },
  "addresses": [
    {
      "address": "0x001ddabba5782ee48842318bd9ff4008647c8d9c",
      "chains": null,
      "source": "x402scan",
      "risk": 0,
      "notes": null,
      "updated": "2026-09-28"
    }
  ]
}
```

- 404 if no facilitator matches `{name}`:
  ```json
  { "detail": "No facilitator named 'nonexistent'." }
  ```

## GET /servers

- auth: `X-API-Key`, `read` or `read_write`
- query params: `limit` (default `50`, max `500`), `offset` (default `0`) --
  there can be thousands of servers, so this is paged rather than returned
  whole the way `GET /facilitators` is
- response: `application/json` -- a page of results, ordered by name

```json
{
  "items": [
    { "name": "2s — the (most) everything API", "risk": 0, "active": true },
    { "name": "api.example.com", "risk": 0, "active": false }
  ],
  "total": 1842,
  "limit": 50,
  "offset": 0
}
```

| field | meaning |
| --- | --- |
| `items` | this page's rows, ordered by name |
| `total` | total number of servers, regardless of paging |
| `limit` | the page size actually used (echoes the request, or the default) |
| `offset` | the offset actually used (echoes the request, or the default) |

To fetch every server, keep requesting with `offset` advanced by `limit`
(or by the number of `items` returned, if fewer) until `offset + len(items)
>= total`.

## GET /servers/{name}

- auth: `X-API-Key`, `read` or `read_write`
- `{name}` is matched against `server.name` case-insensitively
- response: `application/json` -- every column on the server row, its
  `api`/`doc`/`website`/`x402scan` URLs each expanded to the full `uris` row
  (same shape as `GET /facilitators/{name}`), every address linked to it,
  and every service it offers

```json
{
  "id": 12,
  "name": "api.example.com",
  "risk": 0,
  "active": true,
  "notes": null,
  "updated": "2026-09-30",
  "api": {
    "url": "https://api.example.com",
    "ip": "203.0.113.10",
    "location": "US-East (N. Virginia)",
    "subject": "api.example.com",
    "risk": 0,
    "notes": null,
    "updated": "2026-09-30"
  },
  "doc": null,
  "website": null,
  "x402scan": null,
  "addresses": [
    {
      "address": "0xabc1230000000000000000000000000000abc1",
      "chains": null,
      "source": "x402scan",
      "risk": 0,
      "notes": null,
      "updated": "2026-09-30"
    }
  ],
  "services": [
    {
      "id": 501,
      "path": "/weather/forecast",
      "description": "7-day weather forecast",
      "price": "0.02 USD",
      "tags": "weather",
      "category": "weather",
      "active": true,
      "risk": 0,
      "notes": null,
      "updated": "2026-09-30"
    }
  ]
}
```

- 404 if no server matches `{name}`:
  ```json
  { "detail": "No server named 'nonexistent'." }
  ```

## GET /services

- auth: `X-API-Key`, `read` or `read_write`
- query params:
  - `limit` (default `50`, max `500`), `offset` (default `0`) -- same
    paging contract as `GET /servers`, across every server's services
  - `category` -- optional, repeatable (e.g. `?category=weather&category=llm`)
    -- when given, only services whose `category` is one of the listed
    values are returned; `total` reflects the filtered count too, so paging
    through a filtered result set stays consistent. Omit entirely for no
    filtering (all categories).
- response: `application/json` -- a page of results, ordered by server name
  then path

```json
{
  "items": [
    {
      "id": 501,
      "server_name": "api.example.com",
      "path": "/weather/forecast",
      "category": "weather",
      "price": "0.02 USD",
      "active": true
    }
  ],
  "total": 9603,
  "limit": 50,
  "offset": 0
}
```

## GET /services/categories

- auth: `X-API-Key`, `read` or `read_write`
- response: `application/json` -- every distinct, non-null `category`
  value currently assigned to at least one service, sorted. This is the
  option list for `GET /services`' `category` filter above -- not the full
  set of categories `scripts/service_classifier.py` knows how to assign,
  which can include ones no service has actually been classified into yet.

```json
["finance", "llm", "search", "weather"]
```

## GET /services/{id}

- auth: `X-API-Key`, `read` or `read_write`
- `{id}` is the service's numeric id (not unique on its own across
  servers -- a service's `path` is only unique *within* its own server, see
  `db/schema.sql`'s `service_server_path_key` constraint)
- response: `application/json` -- every column on the service row, with
  `server` expanded to `{"id", "name"}` rather than a bare foreign key

```json
{
  "id": 501,
  "path": "/weather/forecast",
  "description": "7-day weather forecast",
  "price": "0.02 USD",
  "tags": "weather",
  "category": "weather",
  "active": true,
  "risk": 0,
  "notes": null,
  "updated": "2026-09-30",
  "server": { "id": 12, "name": "api.example.com" }
}
```

- 404 if no service has that id:
  ```json
  { "detail": "No service with id 999999." }
  ```

## Errors (API-key-protected endpoints)

| status | meaning |
| --- | --- |
| 401 | `X-API-Key` header missing, or the key isn't recognised |
| 403 | key is valid but its access level is insufficient for this endpoint |
| 404 | (a `/{name}` or `/{id}` route only) nothing matches |
| 422 | `limit`/`offset` out of range, or `/services/{id}`'s `{id}` isn't an integer -- FastAPI's own request validation, before this API's own code runs |

401/403/404 all respond with the same shape:

```json
{ "detail": "<message>" }
```

422 is FastAPI's own validation error shape instead -- `detail` is a list,
not a string:

```json
{ "detail": [{ "type": "int_parsing", "loc": ["path", "id"], "msg": "...", "input": "..." }] }
```
