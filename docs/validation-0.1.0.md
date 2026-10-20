# Open Fin 0.1.0 validation

Date: 2026-10-20. This is an experimental, single-owner release.

## Verified

- Python 3.12 Linux container: **188 tests passed**, no skipped cases, networking disabled.
- Ruff lint and format checks passed; Python source compiled successfully.
- Application-owned line coverage: **86%**, including the unimported entrypoints.
  The Santander adapter is 73% covered; browser/navigation branches remain partly
  untested. No application-owned code was excluded from measurement.
- Runtime and test images built from pinned, hashed Python dependencies.
- The installed wheel retains AGPL, Fintself MIT and Playwright Apache notices.
- Docker Compose started the API and durable worker as UID/GID 10001, with a
  read-only root filesystem, owner-only persistent data and a loopback HTTP port.
- A real HTTP demo created a connection, queued a worker job and returned two
  accounts, two currency-specific card products and four synthetic movements.
  The USD movement retained `-14.82` as a decimal string.
- The same HTTP query returned the same data after restarting both services.
- Chromium rendered static HTML with its sandbox enabled inside the actual
  Compose worker. No banking site was contacted.
- A synthetic process test killed a worker while its adapter owned a detached
  child process. The inherited lock remained held until watchdog cleanup, the
  detached process stopped, and an unrelated process survived.

The local macOS tool sandbox blocks process inspection, so its equivalent test
run skips the orphan test. That case passed both in Linux Docker and in an
unrestricted synthetic macOS execution. Unsupported process supervision is
detected before any live bank browser is launched.

The test stack emits one upstream Starlette/httpx deprecation warning. It does
not affect the tested application behavior.

## Reproduce

```sh
make docker-test
make setup  # only on a new checkout; existing secrets are never overwritten
make up
python3 scripts/smoke-demo.py
docker compose restart api worker
python3 scripts/smoke-demo.py --connection-id CONNECTION_ID_FROM_PREVIOUS_OUTPUT
```

The HTTP smoke script refuses live mode. An exhausted local Docker address pool
was handled with an ignored project-only network override; no existing network
or other project was changed.

## Not verified or implemented

- No real Santander credentials were supplied and no real bank was accessed.
- Current portal compatibility, product variants, MFA and complete history remain
  unverified. The adapter reports bounded visible-portal coverage and absent balances.
- Best-effort movement identity without bank IDs cannot reconcile every changed
  description or billed/unbilled transition.
- Shared multi-tenant SaaS, hosted provisioning, customer onboarding, subscriptions,
  billing and commercial service guarantees are roadmap work.

Requirements and exact automated scenario links are in
[the behavior specification](specs/banking/initial-api.md).
