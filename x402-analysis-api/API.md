# x402 Analysis API

Base URL: `https://<your-deployment>.vercel.app`

Auth: send an API key in the `X-API-Key` header. Two access levels:
`read` (read-only endpoints) and `read_write` (read-only and read-write
endpoints; a `read_write` key also satisfies a `read` requirement).
`GET /` and `GET /status` need no key.

## GET /

- auth: none
- response: `text/plain`, one line pointing to this file.

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

## Errors (API-key-protected endpoints)

None exist yet beyond the above. Once added, all of them use:

| status | meaning |
| --- | --- |
| 401 | `X-API-Key` header missing, or the key isn't recognised |
| 403 | key is valid but its access level is insufficient for this endpoint |

```json
{ "detail": "<message>" }
```
