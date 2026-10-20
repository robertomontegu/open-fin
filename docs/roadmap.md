# Roadmap

This is a statement of intent, not a release commitment or a list of production
capabilities. The current scope is defined in [the initial API specification](specs/banking/initial-api.md).

## Initial self-hosted foundation

- A single-owner API for connections, durable synchronization, cached accounts,
  cards and movements.
- A separate worker and SQLite persistence with encrypted bank credentials.
- Currency-specific products, decimal strings, explicit failures and bounded queries.
- Synthetic demo, fixture tests, a reproducible dependency lock and non-root Compose.
- A Santander adapter adapted from Fintself with upstream attribution preserved.

These features are implemented against synthetic data and fixtures. The live adapter
requires validation against the current Santander Chile portal before a stable
release can claim live support. Initial Docker validation covers the image build
and sandboxed Chromium startup without networking. Additional host configurations
still need verification; a browser startup check does not verify bank compatibility.

## Before claiming Santander support

- Validate login, owned products and statements on an operator's local deployment.
- Compare extracted totals, decimal values, currencies and duplicate occurrences
  against bank records without collecting credentials or financial history centrally.
- Document tested portal variants, known MFA requirements and available history.
- Check sandboxed Chromium on Linux and Docker Desktop, and document supported hosts.
- Exercise failure recovery, backups/restores, upgrade compatibility and key handling.
- Establish release notes, issue triage and a private security reporting channel.

Full historical backfill, unattended MFA and every Santander product are not promised.
Further banks should follow the same fixtures and explicit local validation process.

## Self-hosted product improvements

Potential future work includes a small local connection panel, interactive challenge
handling, configurable schedules with conservative login limits, improved account
identity, transactional migrations, key rotation and operator backup tooling. Each
needs its own acceptance criteria before becoming a supported capability.

## Commercial managed hosting

The planned funding model is free self-hosting under AGPL-3.0-only and paid managed
operations. The first version does not implement a managed SaaS, subscriptions or
billing. A commercial service needs separate design and verification for tenant
isolation, authorization, secure onboarding, secret management, observability,
backups, support, incident response and service commitments.

AGPL permits commercial use and hosting by others under its terms. Open Fin's
commercial offering would sell operations and support; it does not reserve an
exclusive right to run the software. Changes offered over a network must meet the
license's corresponding-source requirements.
