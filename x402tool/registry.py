"""A registry of known x402 facilitators.

Data compiled from:
  - https://github.com/Swader/x402facilitators (public community-maintained list), snapshot 2026-09-10
  - Coinbase CDP's x402 Facilitator API reference:
    https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/x402-facilitator

Facilitator infrastructure changes quickly (URLs move, new facilitators launch,
access policies change). Treat this list as a starting point, not ground truth -
use `x402tool supported <id>` to check whether an entry is actually live.

Each entry's "api" field controls how the CLI talks to it:
  - "generic": implements the plain x402 facilitator HTTP contract used by the
    x402 Python/TypeScript SDKs: POST {base_url}/verify, POST {base_url}/settle,
    GET {base_url}/supported.
  - "cdp": Coinbase's authenticated CDP Platform API, which additionally exposes
    bazaar discovery, MCP, and endpoint-validation routes not part of the core
    x402 spec. Base is https://api.cdp.coinbase.com/platform and paths are
    versioned (/v2/x402/verify, /v2/x402/settle, /v2/x402/supported). Requires
    a CDP API key pair (see cdp_auth.py).

Each entry's "auth" field describes what a *generic* facilitator needs for its
verify/settle/supported calls:
  - {"type": "none"}: unauthenticated.
  - {"type": "header", "name": "..."}: pass --api-key, sent as that header.
  - {"type": "bearer"}: pass --api-key, sent as "Authorization: Bearer <key>".
  - {"type": "url-path", "template": "..."}: pass --api-key, substituted into
    the URL template (overrides base_url).
  - {"type": "unknown"}: registry lists this facilitator as gated but does not
    document its auth scheme; use --header to supply credentials if you have
    them.
"""

from __future__ import annotations

from typing import Any

SOURCE_NOTE = (
    "Facilitator list compiled from https://github.com/Swader/x402facilitators "
    "(snapshot 2026-09-10) and the Coinbase CDP x402 API reference. Entries may "
    "be stale - verify with `x402tool supported <id>`."
)

CDP_PLATFORM_NETWORKS = [
    "eip155:8453 (Base)",
    "eip155:84532 (Base Sepolia)",
    "eip155:137 (Polygon)",
    "eip155:42161 (Arbitrum One)",
    "eip155:480 (World Chain)",
    "eip155:4801 (World Chain Sepolia)",
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp (Solana mainnet)",
    "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1 (Solana devnet)",
]

FACILITATORS: dict[str, dict[str, Any]] = {
    "coinbase-cdp": dict(
        name="Coinbase CDP Platform API",
        api="cdp",
        base_url="https://api.cdp.coinbase.com/platform",
        docs_url="https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/x402-facilitator",
        access="gated",
        fee=0,
        networks=CDP_PLATFORM_NETWORKS,
        auth={"type": "cdp-jwt"},
        notes=(
            "CDP's authenticated v2 Platform API. First 1,000 onchain "
            "settlement transactions/month are free, then $0.001 each. Also "
            "exposes bazaar discovery, MCP, and endpoint-validation routes "
            "(see the `cdp` subcommand group) that go beyond the core x402 "
            "spec. Requires a CDP API key pair: CDP_API_KEY_ID / "
            "CDP_API_KEY_SECRET."
        ),
    ),
    "402104": dict(
        name="402104",
        api="generic",
        base_url="https://x402.load.network",
        docs_url="https://x402.load.network",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
    ),
    "aurracloud": dict(
        name="AurraCloud",
        api="generic",
        base_url="https://x402-facilitator.aurracloud.com",
        docs_url="https://x402-facilitator.aurracloud.com",
        access="gated_paid",
        fee=0,
        networks=["base", "solana"],
        auth={
            "type": "url-path",
            "template": "https://x402-facilitator.aurracloud.com/api/v1/{api_key}",
        },
        notes="verify/settle/supported require an API key appended to the URL path.",
    ),
    "canton": dict(
        name="Canton",
        api="generic",
        base_url="https://facilitator.ftptech.xyz",
        docs_url="https://www.ftptech.xyz/x402",
        access="public",
        fee=0,
        networks=["canton"],
        auth={"type": "none"},
        notes="Settles in Canton Coin or USDCx on Canton MainNet, run by FTP Tech LLC.",
    ),
    "celo": dict(
        name="Celo Facilitator",
        api="generic",
        base_url="https://api.x402.celo.org",
        docs_url="https://x402.celo.org",
        access="public",
        fee=0,
        networks=["celo"],
        auth={"type": "none"},
    ),
    "codenut": dict(
        name="CodeNut",
        api="generic",
        base_url="https://facilitator.codenut.ai",
        docs_url="https://docs.codenut.ai/guides/x402-facilitator",
        access="public",
        fee=0,
        networks=["base", "solana"],
        auth={"type": "none"},
    ),
    "daydreams": dict(
        name="Daydreams",
        api="generic",
        base_url="https://facilitator.daydreams.systems",
        docs_url="https://facilitator.daydreams.systems",
        access="public",
        fee=0,
        networks=["base", "solana"],
        auth={"type": "none"},
    ),
    "dexter": dict(
        name="Dexter",
        api="generic",
        base_url="https://facilitator.dexter.cash",
        docs_url="https://facilitator.dexter.cash",
        access="public",
        fee=0,
        networks=["solana"],
        auth={"type": "none"},
    ),
    "fluxa": dict(
        name="FluxA",
        api="generic",
        base_url="https://facilitator.fluxapay.xyz",
        docs_url="https://facilitator.fluxapay.xyz",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
    ),
    "heurist": dict(
        name="Heurist",
        api="generic",
        base_url="https://facilitator.heurist.xyz",
        docs_url="https://docs.heurist.ai/x402-products/facilitator",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
    ),
    "hpp": dict(
        name="HPP",
        api="generic",
        base_url="https://facilitator.hpp.io",
        docs_url="https://docs.hpp.io/x402/facilitator",
        access="public",
        fee=0,
        networks=["eip155:190415 (HPP Mainnet)"],
        auth={"type": "none"},
        notes="Also exposes the exact scheme's 'upto' variant on its own HPP Mainnet L2 (eip155:190415).",
    ),
    "kamiyo": dict(
        name="KAMIYO",
        api="generic",
        base_url="https://kamiyo.ai/api/v1/x402",
        docs_url="https://kamiyo.ai/docs",
        access="gated",
        fee=0,
        networks=["base", "polygon", "solana"],
        auth={"type": "unknown"},
        notes=(
            "Registry marks this facilitator as gated but does not document "
            "its auth scheme; use --header to supply credentials if you have "
            "them."
        ),
    ),
    "meridian": dict(
        name="Meridian Facilitator",
        api="generic",
        base_url="https://api.mrdn.finance/v1",
        docs_url="https://mrdn.finance",
        access="public",
        fee=0,
        networks=[
            "ethereum", "avalanche", "base", "optimism", "arbitrum", "polygon",
            "unichain", "ink", "worldchain", "sei", "hyperevm", "megaeth",
            "tempo", "robinhood", "bsc", "monad", "bot-chain", "solana",
        ],
        auth={"type": "none"},
        notes="Also exposes the exact scheme's 'upto' variant across most listed EVM networks.",
    ),
    "mogami": dict(
        name="Mogami",
        api="generic",
        base_url="https://facilitator.mogami.tech",
        docs_url="https://mogami.tech/",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
    ),
    "near": dict(
        name="Unknown (Near)",
        api="generic",
        base_url="https://x402.mikedotexe.com",
        docs_url="https://x402.mikedotexe.com",
        access="public",
        fee=0,
        networks=["near"],
        auth={"type": "none"},
    ),
    "polymer": dict(
        name="Polymer",
        api="generic",
        base_url="https://api.polymer.zone/v1",
        docs_url="https://docs.polymerlabs.org/docs/build/start",
        access="public",
        fee=0,
        networks=[],
        auth={"type": "none"},
        notes=(
            "Could not confirm supported networks/schemes: a live check of "
            "/supported returned HTTP 400 'Invalid request' from Cloudflare "
            "on every path tried, including '/'  - likely edge-level bot/WAF "
            "protection rather than the API itself. Verify with "
            "`x402tool supported polymer` from a normal client."
        ),
    ),
    "payai": dict(
        name="PayAI",
        api="generic",
        base_url="https://facilitator.payai.network",
        docs_url="https://payai.network",
        access="public",
        fee=0,
        networks=["base", "solana"],
        auth={"type": "none"},
    ),
    "polygon": dict(
        name="Polygon Facilitator",
        api="generic",
        base_url="https://x402.polygon.technology",
        docs_url="https://agentic-docs.polygon.technology/general/x402/intro/",
        access="public",
        fee=0,
        networks=["polygon"],
        auth={"type": "none"},
    ),
    "primer": dict(
        name="Primer",
        api="generic",
        base_url="https://x402.primer.systems",
        docs_url="https://docs.primer.systems/facilitator.html",
        access="public",
        fee=0,
        networks=["base", "skale-base", "robinhood"],
        auth={"type": "none"},
        notes="Supports exact, upto, and batch-settlement schemes.",
    ),
    "questflow": dict(
        name="Questflow",
        api="generic",
        base_url="https://facilitator.questflow.ai",
        docs_url="https://facilitator.questflow.ai",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "bearer"},
        notes=(
            "/supported works unauthenticated; verify/settle may still "
            "require Authorization: Bearer <api_key> per the facilitator's "
            "own SDK config."
        ),
    ),
    "solvador": dict(
        name="Solvador",
        api="generic",
        base_url="https://api.solvador.com",
        docs_url="https://solvador.com",
        access="public",
        fee=0,
        networks=[
            "base", "arbitrum", "optimism", "polygon", "avalanche", "celo",
            "linea", "unichain", "worldchain", "monad", "robinhood",
            "solana", "near", "xrpl", "starknet",
        ],
        auth={"type": "none"},
        notes="Also exposes upto and batch-settlement schemes across most listed EVM networks.",
    ),
    "stellar": dict(
        name="Stellar",
        api="generic",
        base_url="https://channels.openzeppelin.com/x402",
        docs_url="https://channels.openzeppelin.com/x402",
        access="gated",
        fee=0,
        networks=[],
        auth={"type": "unknown"},
        notes=(
            "OpenZeppelin Relayer x402 channel. Every path returned HTTP 401 "
            "Unauthorized during a live, unauthenticated check, including "
            "/supported - could not confirm supported networks/schemes or "
            "the auth scheme it expects. Use --header to supply credentials "
            "if you have them."
        ),
    ),
    "thirdweb": dict(
        name="Thirdweb",
        api="generic",
        base_url="https://api.thirdweb.com/v1/payments/x402",
        docs_url="https://portal.thirdweb.com/payments/x402/facilitator",
        access="gated_paid",
        fee=0,
        networks=["base", "polygon"],
        auth={"type": "header", "name": "x-secret-key"},
        notes="Requires a thirdweb secret key for verify/settle/supported.",
    ),
    "t54": dict(
        name="T54",
        api="generic",
        base_url="https://xrpl-facilitator-mainnet.t54.ai",
        docs_url="https://docs.x402.org/dev-tools/facilitators",
        access="public",
        fee=0,
        networks=["xrpl"],
        auth={"type": "none"},
    ),
    "ultravioletadao": dict(
        name="Ultravioleta DAO",
        api="generic",
        base_url="https://facilitator.ultravioletadao.xyz",
        docs_url="https://facilitator.ultravioletadao.xyz",
        access="public",
        fee=0,
        networks=["base", "solana"],
        auth={"type": "none"},
        notes="Gasless payments.",
    ),
    "fareside": dict(
        name="FareSide",
        api="generic",
        base_url="https://facilitator.x402.rs",
        docs_url="https://x402.rs",
        access="public",
        fee=0,
        networks=["base", "polygon"],
        auth={"type": "none"},
    ),
    "figment": dict(
        name="Figment",
        api="generic",
        base_url="https://api.figment.io/x402",
        docs_url="https://docs.figment.io/reference/x402",
        access="gated",
        fee=0,
        networks=[],
        auth={"type": "header", "name": "apikey"},
        notes=(
            "Behind a Kong API gateway (WWW-Authenticate: Key realm=\"kong\"); "
            "a live, unauthenticated check of /supported returned 401 so "
            "supported networks/schemes could not be confirmed. Pass "
            "--api-key with a Figment API key."
        ),
    ),
}


# Facilitators that were previously known to be operational but are no longer
# reachable or maintained. Kept for reference (e.g. to recognize an id someone
# might still ask about) but excluded from FACILITATORS/list-facilitators.
DEFUNCT_FACILITATORS: dict[str, dict[str, Any]] = {
    "coinbase": dict(
        name="Coinbase (public facilitator)",
        api="generic",
        base_url="https://facilitator.cdp.coinbase.com",
        docs_url="https://docs.cdp.coinbase.com/x402/welcome",
        access="public",
        fee=0,
        networks=["base", "solana"],
        auth={"type": "none"},
        notes=(
            "Coinbase's free, unauthenticated x402 facilitator (implemented the "
            "generic /verify, /settle, /supported contract). Host no longer "
            "resolves. Distinct from the authenticated CDP Platform API, see "
            "'coinbase-cdp'."
        ),
    ),
    "openx402": dict(
        name="OpenX402",
        api="generic",
        base_url="https://open.x402.host",
        docs_url="https://open.x402.host",
        access="public",
        fee=0,
        networks=["base", "solana"],
        auth={"type": "none"},
        notes="TLS certificate no longer matches the host.",
    ),
    "virtuals": dict(
        name="Virtuals Protocol",
        api="generic",
        base_url="https://acpx.virtuals.io",
        docs_url="https://app.virtuals.io",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
        notes="/supported now 404s against what looks like a Strapi CMS, not an x402 facilitator.",
    ),
    "xecho": dict(
        name="xEcho",
        api="generic",
        base_url="https://www.xechoai.xyz",
        docs_url="https://www.xechoai.xyz",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
        notes="Deployment disabled on Vercel (DEPLOYMENT_DISABLED).",
    ),
    "corbits": dict(
        name="Corbits",
        api="generic",
        base_url="https://facilitator.corbits.dev",
        docs_url="https://corbits.dev",
        access="public",
        fee=0,
        networks=["solana"],
        auth={"type": "none"},
        notes="Host no longer resolves.",
    ),
    "auto": dict(
        name="Auto (x402scan)",
        api="generic",
        base_url="https://facilitators.x402scan.com",
        docs_url="https://facilitators.x402scan.com",
        access="public",
        fee=0,
        networks=["base"],
        auth={"type": "none"},
        notes="Routes to whichever underlying facilitator x402scan selects.",
    ),
}


class UnknownFacilitatorError(KeyError):
    pass


def get(facilitator_id: str) -> dict[str, Any]:
    entry = FACILITATORS.get(facilitator_id)
    if entry is None:
        known = ", ".join(sorted(FACILITATORS))
        raise UnknownFacilitatorError(
            f"Unknown facilitator '{facilitator_id}'. Known facilitators: {known}"
        )
    return entry


def list_ids() -> list[str]:
    return sorted(FACILITATORS)


def resolve_generic_target(
    entry: dict[str, Any],
    api_key: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Resolve the base URL and headers to use for a generic facilitator call."""
    auth = entry.get("auth", {"type": "none"})
    base_url = entry["base_url"]
    headers: dict[str, str] = {}
    auth_type = auth.get("type")
    if auth_type == "header" and api_key:
        headers[auth["name"]] = api_key
    elif auth_type == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "url-path" and api_key:
        base_url = auth["template"].format(api_key=api_key)
    if extra_headers:
        headers.update(extra_headers)
    return base_url, headers
