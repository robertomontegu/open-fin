# ADR 0001 — Open core and deployment boundary

- Date: 2026-10-20
- Status: Accepted for 0.1.0

## Context and drivers

Open Fin provides an open-source self-hosted banking API and will fund maintenance
through a managed commercial service. Initial users connect their own Santander Chile
products. Credentials and browser automation need explicit deployment isolation.

## Decision

License original work under AGPL-3.0-only; retain the MIT notice for reused Fintself
selectors and flow logic. Build a Python/FastAPI API with a separate durable worker,
SQLite storage and an encrypted credential vault. Run one owner per deployment.
Expose source/version information. Commercial hosting is an operational offering;
multi-tenant authentication, billing, hosted onboarding, monitoring and contractual
service commitments are roadmap items, not implemented capabilities.

## Alternatives

- Direct Fintself dependency: rejected because product/source identity and failure
  semantics require changes inside its Santander extraction.
- A broad fork of all banks: rejected for this release because only Santander can
  be validated with the initial owner's products.
- Shared multi-tenant SaaS immediately: deferred until authorization and isolation
  have their own verified requirements and tests.
- MIT licensing: user chose AGPL-3.0 to preserve openness of network modifications.

## Consequences

Maintain our Santander adapter, fixtures and release validation. Decimal strings and
separate per-currency products form the public contract. A hosted deployment must
offer its corresponding source in accordance with its license. AGPL permits commercial
use; this is not an exclusive right to host. SQLite and its single worker limit
horizontal scaling. Moving to shared hosting requires a separate design decision.

## Affected specifications

- [Initial API](../specs/banking/initial-api.md)

Supersedes: none. Superseded by: none.
