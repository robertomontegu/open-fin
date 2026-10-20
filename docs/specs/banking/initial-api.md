# Initial banking API

Status: implementation contract for 0.1.0. Live Santander compatibility is unverified.

## Purpose and actors

One owner operates one deployment and connects only their own Santander Chile accounts.
Every banking endpoint requires a deployment bearer token. Hosted multi-tenant operation,
billing, payment initiation and arbitrary third-party account access are future work.

## Requirements and acceptance scenarios

### OF-REQ-001 — Local ownership and secret boundaries

Bank credentials SHALL be encrypted at rest using a deployment key, never included in
responses, validation errors or logs. The token and key SHALL be supplied locally.

#### OF-AC-001 — Authentication and validation

Missing/wrong bearer tokens return 401. Invalid request input returns 422 without
echoing bank credentials. Health is public and reveals no bank data. Production docs
and OpenAPI are protected by the same token.

API and worker startup SHALL read the deployment environment and reject missing or
invalid configuration before serving banking requests or launching a connector. A
deployment token of at least 32 characters and a valid Fernet key are required; neither
is generated implicitly at startup or included in configuration representations.

#### OF-AC-002 — Connection lifecycle

An authenticated owner can create/list/delete Santander connections. The persisted
credential ciphertext does not contain the original credentials. Deleting a connection
also removes cached products, movements and sync jobs.

### OF-REQ-002 — Durable, explicit synchronization

A POST sync request SHALL persist a queued job and return 202. A separate worker
executes the job outside the API process. One job per connection can be queued/running.
No automatic bank-login retries occur. The live connector runs in a short-lived
subprocess, receiving bank credentials through stdin rather than command arguments or
environment variables. The child does not inherit the API token or encryption key.

One POSIX worker file lock serializes execution for the database. Its descriptor is
inherited by the live adapter, so an interrupted parent cannot release exclusivity
while its adapter remains alive. Parent timeout/interruption cleanup terminates the
adapter process group and inspects descendant processes, including detached browser
sessions. The child has a finite watchdog and checks process-control availability
before launching the connector. A worker that acquires the lock marks abandoned
running jobs failed without automatically retrying their bank connection.

#### OF-AC-003 — Synchronization and product identity

A successful job atomically stores separate currency-specific accounts/cards and
their movements. Every movement references its product, currency and source. Amounts
serialize as strings preserving decimal cents. Cached data survives service restart.

#### OF-AC-004 — Idempotency and legitimate duplicates

Re-importing the same snapshot SHALL not create extra rows. Two distinct occurrences
with the same date/description/amount/source SHALL both survive. In the absence of
bank transaction IDs, deterministic occurrence identities are best-effort; mutable
descriptions and a transaction moving between statement sources are documented limits.

#### OF-AC-005 — Failures, conflicts and cooldown

Concurrent sync requests return 409; cooldown returns 429 with Retry-After. A failed
or invalid snapshot leaves all previous data unchanged and marks the job failed with
a safe code. API clients can distinguish an empty successful snapshot from failure.

The cooldown is per connection, defaults to 3,600 seconds, and may be explicitly set
to zero. Enqueue records an attempt time; claiming the job updates that time to the
actual start, including when a queued job was delayed. Cooldown is measured from that
stored attempt time. It is not a bank-wide, RUT-wide or cross-deployment login limit.

### OF-REQ-003 — Bounded consultation

#### OF-AC-006 — Queries and deletion boundaries

GET /v1/accounts, /v1/cards and /v1/movements support connection filtering. Movements
add product, date, currency, source and bounded pagination filters. Unknown identifiers
return 404. Deleting while a job is running returns 409. Inverted date ranges return 422.

### OF-REQ-004 — Inspectable, reproducible self-hosting

#### OF-AC-007 — Docker and demo

Compose starts an API and a worker sharing an owner-only persistent volume. Generated
secrets are excluded from Git. Demo mode supplies synthetic Santander data through the
same API/job/storage flow and is clearly labeled. It never accepts real credentials.
Live mode never falls back to demo data. The image runs as a non-root user.

Each connection persists its creation mode (`demo` or `live`) and exposes that mode
as metadata. Changing the deployment mode does not convert existing connections.
Synchronization in another mode returns 409; workers reject a mismatched job before
decrypting credentials or invoking a connector. Account, card and movement queries
show only cache belonging to the current mode, and explicit cross-mode queries return
409. Connection metadata remains visible to the owner so old connections can be
identified and deleted.

Demo accepts only the documented synthetic credential pair and labels its snapshots
`synthetic_demo`. Live failures cannot produce demo results. The direct Python
configuration defaults to live; the generated environment and packaged Compose setup
explicitly select demo for onboarding. `scripts/init-env.py` creates a private 0600
environment file, prints no secrets and refuses to overwrite an existing file.
Configuration requires persistent storage, a finite positive poll interval, a
30–900 second job timeout and an explicit valid headless boolean when supplied.

### OF-REQ-005 — Santander extraction correctness

#### OF-AC-008 — Strict financial parsing and distinct products

The Santander adapter SHALL retain CLP/USD, product identity, billed/unbilled source
and legitimate repeated rows. Invalid monetary/date values and missing expected
tables SHALL fail rather than become zero amounts or empty successful results.
It SHALL not send credentials to any service other than Santander's login flow.
It SHALL close the browser on all outcomes and never persist browser profiles/screenshots.

## Traceability

Automated evidence uses synthetic credentials, bank fixtures and isolated databases.
Parameterized node IDs below denote all cases belonging to the named test function.
They do not establish live bank compatibility, historical completeness or MFA support.

| Acceptance scenario | Exact executable test nodes | Evidence boundary |
| --- | --- | --- |
| OF-AC-001 | `tests/test_api.py::test_health_is_public_and_exposes_no_bank_data`<br>`tests/test_api.py::test_banking_and_documentation_require_valid_token`<br>`tests/test_api.py::test_authenticated_documentation_is_available`<br>`tests/test_api.py::test_non_ascii_authorization_header_returns_401`<br>`tests/test_api.py::test_validation_errors_do_not_echo_credentials`<br>`tests/test_api.py::test_invalid_connection_payload_is_rejected_without_request_echo`<br>`tests/test_config.py::test_missing_secrets_fail_without_generating_replacements`<br>`tests/test_config.py::test_settings_repr_does_not_disclose_deployment_secrets`<br>`tests/test_config.py::test_api_boot_reads_environment_and_refuses_missing_secrets`<br>`tests/test_config.py::test_init_env_cli_creates_private_secrets_and_never_overwrites`<br>`tests/test_config.py::test_credential_models_redact_secret_representations`<br>`tests/test_worker_process.py::test_live_child_receives_bank_credentials_only_over_stdin`<br>`tests/test_worker_process.py::test_adapter_entrypoint_never_prints_raw_exception_or_credentials`<br>`tests/test_worker_process.py::test_blocked_process_control_rejects_before_adapter_or_watchdog` | Synthetic HTTP/startup and secret-boundary checks; no external security audit. |
| OF-AC-002 | `tests/test_api.py::test_connections_are_encrypted_at_rest_and_responses_are_redacted`<br>`tests/test_api.py::test_delete_cascades_cache_and_jobs_but_preserves_other_connections` | Local SQLite encryption and cascade checks; volume/backups remain operator-owned. |
| OF-AC-003 | `tests/test_api.py::test_queued_job_is_durable_and_demo_worker_stores_data`<br>`tests/test_api.py::test_products_preserve_currency_identity_and_decimal_amounts`<br>`tests/test_api.py::test_cached_data_survives_application_restart`<br>`tests/test_santander.py::test_snapshot_keeps_multiple_products_currencies_and_sources` | Synthetic storage/restart and connector orchestration; live product coverage unverified. |
| OF-AC-004 | `tests/test_api.py::test_repeated_import_is_idempotent_and_keeps_legitimate_duplicates`<br>`tests/test_api.py::test_bank_movement_id_deduplicates_captures_and_preserves_identity_on_correction` | Synthetic occurrence/bank-ID cases; mutable bank descriptions remain a known limit. |
| OF-AC-005 | `tests/test_api.py::test_only_one_job_can_be_queued_per_connection`<br>`tests/test_api.py::test_invalid_snapshot_rolls_back_products_and_movements`<br>`tests/test_api.py::test_unexpected_connector_error_is_safe_and_does_not_erase_cache`<br>`tests/test_api.py::test_empty_snapshot_is_successful_and_distinguishable_from_failure`<br>`tests/test_api.py::test_worker_recovers_abandoned_job_without_retrying_connector`<br>`tests/test_api.py::test_product_currency_and_source_mismatch_preserves_previous_snapshot`<br>`tests/test_api.py::test_cooldown_returns_retry_after`<br>`tests/test_api.py::test_cooldown_starts_when_worker_claims_a_delayed_queued_job`<br>`tests/test_worker_process.py::test_timeout_terminates_child_group_and_drains_the_process`<br>`tests/test_worker_process.py::test_interruption_terminates_child_before_reraising`<br>`tests/test_worker_process.py::test_child_failure_only_returns_known_safe_codes`<br>`tests/test_worker_process.py::test_success_exit_with_invalid_snapshot_fails_safely`<br>`tests/test_worker_process.py::test_watchdog_still_terminates_adapter_when_descendant_inspection_fails`<br>`tests/test_worker_process.py::test_parent_cleanup_still_terminates_group_when_descendant_inspection_fails`<br>`tests/test_worker_process.py::test_orphan_watchdog_holds_lock_until_detached_descendants_stop` | Synthetic rollback, cooldown, lock and process checks; process inspection skip described below. |
| OF-AC-006 | `tests/test_api.py::test_running_job_blocks_new_sync_and_connection_deletion`<br>`tests/test_api.py::test_list_filters_keep_connections_and_products_separate`<br>`tests/test_api.py::test_movement_filters_preserve_dates_currency_and_source`<br>`tests/test_api.py::test_movement_pagination_is_stable_and_reports_filtered_total`<br>`tests/test_api.py::test_invalid_movement_filters_return_422`<br>`tests/test_api.py::test_unknown_connection_filter_returns_404`<br>`tests/test_api.py::test_unknown_product_sync_and_delete_return_404` | Synthetic authenticated query and concurrency checks. |
| OF-AC-007 | `tests/test_api.py::test_demo_mode_rejects_real_credentials_without_echoing_them`<br>`tests/test_api.py::test_worker_mode_mismatch_fails_before_decrypting_or_fetching`<br>`tests/test_api.py::test_live_worker_uses_live_boundary_and_never_demo_connector`<br>`tests/test_api.py::test_live_api_hides_demo_cache_and_rejects_explicit_cross_mode_queries`<br>`tests/test_config.py::test_from_env_defaults_require_explicit_secrets`<br>`tests/test_config.py::test_from_env_applies_explicit_deployment_configuration`<br>`tests/test_config.py::test_invalid_deployment_configuration_fails_before_startup`<br>`tests/test_config.py::test_documented_timeout_and_cooldown_boundaries_are_accepted`<br>`tests/test_config.py::test_invalid_environment_values_are_not_silently_accepted`<br>`tests/test_config.py::test_worker_cli_refuses_missing_boot_configuration`<br>`tests/test_config.py::test_init_env_cli_creates_private_secrets_and_never_overwrites`<br>`tests/test_config.py::test_demo_connector_rejects_every_non_demo_credential_pair`<br>`tests/test_config.py::test_demo_snapshot_is_explicitly_synthetic_and_financially_typed` | Synthetic modes/configuration/CLI checks; Docker and Chromium runtime require separate validation. |
| OF-AC-008 | `tests/test_config.py::test_nonfinite_money_cannot_enter_connector_models`<br>`tests/test_santander.py::test_strict_decimal_parsing`<br>`tests/test_santander.py::test_invalid_money_never_becomes_zero`<br>`tests/test_santander.py::test_strict_dates_and_bounded_recent_year_inference`<br>`tests/test_santander.py::test_invalid_dates_fail`<br>`tests/test_santander.py::test_usd_sources_installments_and_repeated_occurrences_survive`<br>`tests/test_santander.py::test_account_charge_payment_and_zero_are_preserved`<br>`tests/test_santander.py::test_bad_rows_fail_the_snapshot_instead_of_skipping`<br>`tests/test_santander.py::test_products_distinguish_currency_kind_and_full_account_reference`<br>`tests/test_santander.py::test_credentials_only_allow_santander_https_origins`<br>`tests/test_santander.py::test_untrusted_origins_fail_before_credentials_are_filled`<br>`tests/test_santander.py::test_login_does_not_fill_credentials_into_redirected_iframe`<br>`tests/test_santander.py::test_browser_requests_cannot_send_credentials_to_external_services`<br>`tests/test_santander.py::test_fetch_closes_browser_and_sanitizes_failures`<br>`tests/test_santander.py::test_missing_table_cannot_be_empty_success`<br>`tests/test_santander.py::test_explicit_bank_empty_state_is_success`<br>`tests/test_santander.py::test_hidden_empty_state_cannot_hide_missing_products`<br>`tests/test_santander.py::test_card_identity_must_be_verified_not_assumed_from_position`<br>`tests/test_santander.py::test_duplicate_card_last4_is_ambiguous`<br>`tests/test_santander.py::test_deadline_is_explicit_and_never_retries_login`<br>`tests/test_santander.py::test_snapshot_keeps_multiple_products_currencies_and_sources`<br>`tests/test_santander.py::test_table_extraction_keeps_duplicate_dom_rows`<br>`tests/test_santander.py::test_refresh_wait_only_accepts_bank_transaction_responses`<br>`tests/test_santander.py::test_failed_bank_response_cannot_produce_success` | Synthetic financial parsing and browser mocks; live Santander and MFA unverified. |

Process lifecycle tests include a synthetic detached browser process. The real
orphan-watchdog integration case is skipped on hosts that deny process inspection;
a skipped run is not evidence that descendant cleanup works on that host. The Linux
test image must execute it without that skip to verify its runtime process boundary.

## Known limits

Only rows visible in the portal are supported initially; complete historical backfill
is not promised. Each card/currency is a separate API product. No balance is invented
for live products; their current adapter balance is null. Statement periods and
historical pagination are not selected. Dates without a year use a recent-history
inference in the America/Santiago timezone. Cards identified only by their last four
digits fail if those digits are ambiguous.

There is no challenge/resume API or persistent bank session. An unsupported MFA or
CAPTCHA challenge fails safely and needs a later explicit synchronization; successful
challenge handling has not been verified. Browser HTTP traffic is restricted to
Santander HTTPS hosts, which may reject a portal flow requiring other domains.

Fixtures verify parsing, product separation, safe failure behavior and browser cleanup
without contacting Santander. API tests verify persistent jobs/cache, isolation,
rollback and consultation against synthetic connectors. The process tests verify
supervision with synthetic processes or mocks; they do not exercise a real Chromium
bank session. Docker build, Chromium sandbox operation and live login require their
own runtime validation and are not established by these fixtures.

Credentials are encrypted in SQLite; financial history remains readable to the local
operator. Data files must be protected by the operator's filesystem/volume encryption
and backups. File locking and process supervision assume the POSIX/Linux deployment
boundary; multi-host worker coordination is outside this version's scope.

## Related decisions

- [0001 — Open core and deployment boundary](../../adrs/0001-open-core-and-deployment.md)
