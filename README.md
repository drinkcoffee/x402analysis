# x402analysis

Tools and analysis for the [x402](https://www.x402.org/) payment protocol ecosystem.

## Sub-projects

- **[cli-tool/](cli-tool/)** — `x402tool`, a command-line tool for analyzing
  the x402 ecosystem: a registry of known facilitators, exercising their
  HTTP APIs (verify, settle, supported, Coinbase's bazaar-discovery
  extensions), and a set of standalone analysis commands (facilitator
  infrastructure fingerprinting, blockchain address screening, an
  x402scan.com scraper, and more). See
  [cli-tool/README.md](cli-tool/README.md) for full usage.

- **[x402-analysis-gui/](x402-analysis-gui/)** — a minimal Auth0-gated
  website deployed on Vercel: a public landing page at `/`, and an
  authenticated `/dashboard` page. See
  [x402-analysis-gui/README.md](x402-analysis-gui/README.md) for setup and
  deployment.
