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

## Errors (API-key-protected endpoints)

| status | meaning |
| --- | --- |
| 401 | `X-API-Key` header missing, or the key isn't recognised |
| 403 | key is valid but its access level is insufficient for this endpoint |
| 404 | (GET /facilitators/{name} only) no facilitator matches `{name}` |

```json
{ "detail": "<message>" }
```
