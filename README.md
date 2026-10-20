# Open Fin

**Your bank connections, in your own infrastructure.** An open-source banking API
for people connecting their own accounts. The first adapter targets Santander Chile.

> **Early development — live banking is unverified.** The demo is synthetic. Passing
> fixture tests does not establish compatibility with Santander's current portal,
> MFA, every product, or complete transaction history. Do not depend on this version
> for unattended financial operations.

One owner runs one deployment. Open Fin stores encrypted bank credentials, queues
durable synchronization jobs, and serves cached accounts, cards and movements through
a bearer-authenticated API. An independent worker performs extraction. CLP and USD
products remain separate and amounts are decimal strings.

**En español:** una API bancaria abierta y autohospedada; cada persona conecta sus
propias cuentas y conserva sus datos en su infraestructura. La primera versión se
centra en Santander Chile. La conexión real aún requiere validación.

## Run the synthetic demo

Requirements: Docker Engine or Docker Desktop with Docker Compose v2, Python 3
for local setup, and `curl`. Linux is the initial container target.

```sh
git clone https://github.com/robertomontegu/open-fin.git
cd open-fin
make setup
make up
curl --fail http://127.0.0.1:8088/health
```

`make setup` creates `.env` exclusively, with file mode `0600`, a random API token
and a Fernet encryption key. It never prints either secret and refuses to overwrite
an existing file. Keep `.env` private. `make up` starts the API and one worker from
the same image. The API listens on `127.0.0.1:8088` only. No bank is contacted in demo
mode. The named volume `bank-data` persists across `make down` and restarts.

If Docker reports `all predefined address pools have been fully subnetted`, create
an ignored local `compose.override.yaml` with an explicit free subnet for this
project. Choose a range that does not overlap your Docker, LAN or VPN networks;
leave existing projects untouched. For example:

```yaml
networks:
  default:
    ipam:
      config:
        - subnet: 10.203.0.0/24
```

After startup, `python3 scripts/smoke-demo.py` checks the real HTTP API, worker,
synthetic accounts/cards and decimal amounts. It refuses to run against live mode.
Its output includes a connection ID; use `--connection-id ID` after restarting
to check that the cached snapshot persisted.

Load the generated token in your local shell and define a helper that
passes the token to curl through standard input instead of its command arguments:

```sh
OPEN_FIN_API_TOKEN=$(python3 -c 'from pathlib import Path; print(next(line.split("=", 1)[1] for line in Path(".env").read_text().splitlines() if line.startswith("OPEN_FIN_API_TOKEN=")))')
export OPEN_FIN_API_TOKEN
API=http://127.0.0.1:8088
auth_curl() {
  printf 'header = "Authorization: Bearer %s"\n' "$OPEN_FIN_API_TOKEN" |
    curl --silent --show-error --fail-with-body --config - "$@"
}
```

Create a demo connection. **Only** the synthetic credentials below are accepted
in demo mode; supplying real bank credentials is rejected.

```sh
CONNECTION_ID=$(auth_curl "$API/v1/connections" \
  --header 'Content-Type: application/json' \
  --data '{"bank":"santander","label":"My demo","credentials":{"rut":"11111111-1","password":"demo"}}' |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

JOB_ID=$(auth_curl --request POST "$API/v1/connections/$CONNECTION_ID/sync" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

auth_curl "$API/v1/syncs/$JOB_ID"
```

Synchronization returns `202` with a persisted job. Repeat the last request until
the worker reports completion, then inspect the cached products and movements:

```sh
auth_curl "$API/v1/accounts?connection_id=$CONNECTION_ID"
auth_curl "$API/v1/cards?connection_id=$CONNECTION_ID"
auth_curl "$API/v1/movements?connection_id=$CONNECTION_ID&limit=50&offset=0"

# After the job finishes, verify the same data survives a restart.
docker compose restart api worker
docker compose up -d --wait
auth_curl "$API/v1/movements?connection_id=$CONNECTION_ID&limit=50&offset=0"
```

Product and movement lists return `items`, `total`, `limit` and `offset`. Monetary values are strings,
for example `"-17.14"`, with a currency on each movement. A successful empty result
is distinct from a failed sync. The protected API schema is available locally:

```sh
auth_curl "$API/openapi.json" | python3 -m json.tool
```

## API and synchronization behavior

| Operation | Endpoint |
| --- | --- |
| Deployment health, mode and version | `GET /health` |
| Public source, license and version information | `GET /` |
| Create or list your connections | `POST /v1/connections`, `GET /v1/connections` |
| Delete a connection and its cached data | `DELETE /v1/connections/{id}` |
| Queue a synchronization | `POST /v1/connections/{id}/sync` |
| Inspect a durable job | `GET /v1/syncs/{id}` |
| Read cached products | `GET /v1/accounts`, `GET /v1/cards` |
| Read cached movements | `GET /v1/movements` |

Every banking endpoint and the API documentation require the deployment bearer
token. The health endpoint exposes no bank data. Filters and bounds are described
in the OpenAPI schema and [behavior specification](docs/specs/banking/initial-api.md).

Only one job per connection can be queued or running. Conflicts return `409`;
the login cooldown returns `429` with `Retry-After`. The worker does not retry bank
logins automatically. Failed snapshots leave previous cached data unchanged. On
worker restart, abandoned running jobs fail safely instead of replaying logins.
Run exactly one worker per deployment.

Re-importing the same snapshot preserves duplicate occurrences without adding more
rows. Without stable bank transaction IDs, matching is best-effort: changed
descriptions or a purchase moving from unbilled to billed statements may produce
separate records. Only visible portal history is initially supported. Full historical
backfill and continuous unattended synchronization are not promised.

## Trying your own Santander connection

Live integration is **unverified**. Use only your own Santander Chile products.
Real bank credentials belong in your own local client, never in GitHub issues,
chat, screenshots, `.env`, shell history, or this project's maintainers' systems.

1. Stop the demo with `make down`.
2. In your local `.env`, change `OPEN_FIN_MODE=live`; keep the generated token/key
   and `OPEN_FIN_COOLDOWN_SECONDS=3600`. Restart with `make up`.
3. Create a connection with your local client over loopback. This example prompts
   without echoing credentials or putting them in command arguments or files:

```sh
python3 - <<'PY'
import getpass
import json
import os
import urllib.request

payload = {
    "bank": "santander",
    "label": "My Santander",
    "credentials": {
        "rut": getpass.getpass("Santander RUT: "),
        "password": getpass.getpass("Santander password: "),
    },
}
request = urllib.request.Request(
    "http://127.0.0.1:8088/v1/connections",
    data=json.dumps(payload).encode(),
    headers={
        "Authorization": "Bearer " + os.environ["OPEN_FIN_API_TOKEN"],
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    print("Connection ID:", json.load(response)["id"])
PY
```

The earlier token-loading step must run in the same shell as this client. Queue
and inspect a sync using the returned connection ID as in the demo. Connections retain their creation mode. Live queries exclude cached demo data, and
a connection created in another mode cannot be synchronized. Live mode never
substitutes synthetic results. An authentication challenge or portal change can
make a sync fail. There is no remote MFA interface in this release; interactive
bank challenges may require manual approval and a later synchronization.

The container uses non-root Chromium with its sandbox enabled and a
[Playwright-derived seccomp profile](scripts/chromium-seccomp.json). The host must
support unprivileged user namespaces. Some host kernels, container engines and
AppArmor policies block them even with the profile; that environment needs operator
configuration before live extraction can work. No privileged container or sandbox
disable flag is supplied. Docker's default setup has no display: keep
`OPEN_FIN_HEADLESS=true`. Headed local development needs a display server.

The worker must also be able to inspect and stop its own child processes. If a
host sandbox blocks that capability, live jobs fail before browser startup with
`process_control_unavailable`. Run the supported Linux container environment or
adjust the host's process-inspection permissions before attempting a bank login.

For remote access, use an SSH tunnel to the loopback listener or configure your own
authenticated HTTPS reverse proxy. Do not expose the plain HTTP port publicly.

## Configuration and data

| Variable | Purpose / default |
| --- | --- |
| `OPEN_FIN_API_TOKEN` | Required secret protecting the deployment |
| `OPEN_FIN_ENCRYPTION_KEY` | Required Fernet key encrypting bank credentials |
| `OPEN_FIN_DATABASE_PATH` | SQLite path; Compose fixes it to `/data/open-fin.db` |
| `OPEN_FIN_MODE` | `demo` by default; explicitly choose `live` for the bank |
| `OPEN_FIN_COOLDOWN_SECONDS` | Minimum interval between login attempts; `3600` |
| `OPEN_FIN_JOB_TIMEOUT_SECONDS` | Maximum job execution time; `240` |
| `OPEN_FIN_PORT` | Loopback host port; `8088` |
| `OPEN_FIN_HEADLESS` | `true` by default |
| `OPEN_FIN_SOURCE_URL` | Corresponding source URL, set to your fork when modified |

Both processes run as UID/GID `10001`, share the owner-only `/data` directory, and
keep temporary browser state in memory-backed `/tmp`. Browser profiles and debug
screenshots are not persisted. The database holds financial history in plaintext;
only bank credentials receive application-level encryption. Protect the host,
volume and backups with filesystem/storage encryption and access controls.

Back up the SQLite database and encryption key together while the services are
stopped, using your infrastructure's backup mechanism. Losing the key makes stored
credentials unreadable. Key rotation is not implemented: changing it does not
re-encrypt existing connections. `docker compose down --volumes` permanently removes
the local data volume. See [SECURITY.md](SECURITY.md) for the deployment boundary.

## Development

Use Python 3.12 and install the pinned development environment:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
make check
```

`make docker-test` builds and runs fixture tests with networking disabled. CI checks
lint, tests, bytecode compilation and wheel/container builds. No CI job logs in to
a bank or needs bank credentials. Installing Chromium locally is only necessary
for deliberate live validation: `python -m playwright install --with-deps chromium`.

The [initial API specification](docs/specs/banking/initial-api.md) defines acceptance
criteria. [ADR 0001](docs/adrs/0001-open-core-and-deployment.md) records the architecture
and commercial boundary. Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing a
connector.

The [0.1.0 validation record](docs/validation-0.1.0.md) reports the 188 passing
container tests, 86% line coverage, real HTTP demo/restart checks and the remaining
live-bank validation gap.

## Project and license

Open Fin's self-hosted software is free to use under **AGPL-3.0-only**. Commercial
managed hosting is a future funding path for maintenance; onboarding, subscriptions,
billing, tenant isolation and service guarantees are not implemented. The license
allows commercial use and other operators can offer hosting under its terms. See
the [roadmap](docs/roadmap.md) for the planned validation and product work.

The Santander adapter adapts selectors and flow from
[Fintself](https://github.com/jorgeortizfuentes/fintself), retaining its MIT notice.
The Chromium seccomp profile comes from Microsoft Playwright under Apache-2.0.
See [LICENSE](LICENSE), [NOTICE](NOTICE) and [third_party](third_party). A modified
network deployment must provide the corresponding source as required by the AGPL;
set `OPEN_FIN_SOURCE_URL` to the actual source of your deployment.

Open Fin is independent and is not affiliated with Fintoc, Santander, or Fintself.
