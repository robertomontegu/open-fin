# Contributing

Open Fin starts with one owner per deployment and Santander Chile. Improvements
should make that bounded flow correct, inspectable and easy to operate before
adding more banks or shared hosting.

## Development workflow

Use Python 3.12. Install `requirements.lock`, then install the project with
`python -m pip install --no-deps -e .`. Run `make check` before submitting a change.
`make docker-test` runs the container test suite without networking. Test fixtures
must be synthetic and must not contain production account numbers or credentials.
CI does not contact banks and must not request bank credentials or live secrets.

Keep behavioral requirements and test references aligned in
[the initial API specification](docs/specs/banking/initial-api.md). Record a new
architecture decision when changing the licensing, data, deployment or ownership
boundary. Document uncertainty instead of claiming live compatibility from mocks.

## Connector changes

- Keep CLP/USD, product identity and billed/unbilled sources distinct. Use decimal
  amounts; never silently turn an invalid financial value into zero.
- Preserve genuine duplicate rows while keeping repeated imports idempotent.
- Missing tables, authentication failures and malformed snapshots must fail safely,
  without replacing previously cached data with an empty or partial snapshot.
- Do not log credentials, bank HTML, account numbers, browser profiles or financial
  screenshots. Return stable error codes rather than raw bank responses.
- Keep browser lifetimes bounded and close sessions on every outcome. Preserve
  Chromium sandboxing and do not introduce privileged containers.
- Use only the account owner's credentials, submitted to the bank login flow. Do
  not add third-party credential, analytics or error-reporting services.
- Live validation is an explicit local operator task. Never send a maintainer
  credentials or add a real account to a public fixture or GitHub Actions secret.

Describe the problem, resulting behavior, fixture coverage and any live validation
limits in your pull request. For a selector change, provide sanitized structural
evidence or synthetic HTML. Never attach financial screenshots or portal dumps.

## License and attribution

Contributions are accepted under AGPL-3.0-only. Preserve the upstream MIT notice
for adapted Fintself code and the Apache-2.0 notice for the Playwright seccomp
profile. Add appropriate provenance and license text for other reused work in
`NOTICE` and `third_party/`. Network deployments of modified versions must satisfy
the corresponding-source requirements of the AGPL.
