# Local Santander validation flow evidence

Date: 2026-10-20. Scope: the owner-operated terminal flow defined by
[OF-REQ-006](specs/banking/local-live-validation.md). This record contains no bank
credentials or financial results.

## Automated evidence

The final test image passed **342 tests**, without skips, with networking disabled:

```sh
docker build --target test -t open-fin:test .
docker run --rm --init --network none \
  -e COVERAGE_FILE=/tmp/open-fin.coverage open-fin:test sh -c \
  'python -m ruff check --no-cache src tests scripts &&
   python -m ruff format --check --no-cache src tests scripts &&
   python -m pytest -p no:cacheprovider --cov=open_fin --cov-report=term-missing &&
   PYTHONPYCACHEPREFIX=/tmp/open-fin-pycache python -m compileall -q src scripts'
```

Owned executable line coverage was **89%** (1,533 statements, 176 uncovered).
The local wizard measured 84%; the report module measured 97%. Uncovered live
browser branches, process-control paths and CLI error paths remain gaps; this
measurement is not a claim of complete coverage. One upstream Starlette/httpx
deprecation warning was present.

The synthetic checks cover redirect refusal, proxy bypass, hidden terminal entry,
private artifacts, safe diagnostics, uncertain mutation outcomes, one recorded job,
resumption, complete API pagination and current-snapshot filtering. Mocked Compose
checks establish configuration preservation and redacted failures. Report tests
verify escaping, financial precision, product references, cached-row exclusion and
the distinction between automatic checks and the owner's comparison. They inspect
HTML structure; no visual browser verification is claimed.

The runtime image also built successfully. macOS system Python 3.9 ran the CLI help
and preparation command successfully. Preparation started the local Compose stack;
the public health response reported `status=ok` and `mode=live`. Preparation does not
submit bank credentials or enqueue a bank synchronization.

## Live evidence boundary

The live trial requires the owner to enter credentials privately and compare the
local report with Santander. The coding agent may inspect only the allowlisted
`data/validation/santander/status.json` summary after entry. The private report,
database, credentials and bank browser session are outside that inspection scope.

Green tests and a healthy live-mode deployment establish readiness for the trial,
not a successful Santander login, MFA compatibility, complete product coverage or
correct bank movements. Live compatibility remains unverified until the owner
finishes the local comparison. No financial report is committed or uploaded.

## Public portal follow-up

The public page in the owner's in-app browser exposes its entry as a link named
`Abrir panel de ingreso`. The adapter now supports that exact visible link and
the earlier button, rejecting absent or ambiguous entries. HTTP 401/403 and other
HTTP errors are classified before selectors or credential input.

A fresh isolated browser without credentials received HTTP 403 from the public
portal; an unauthenticated HTTP request from the container also received 403,
while a request from the host received 200. A direct container request without
environment proxy discovery did not complete. These observations establish an
unresolved access difference between environments; they do not establish its cause
or a successful authenticated connection. No bank login was retried during these
public diagnostics. The updated runtime is healthy in live mode, and the recorded
trial remains failed rather than being reset by a container rebuild.
