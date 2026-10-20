# Local Santander live validation

Status: implementation contract for the first owner-operated live trial. Automated
evidence uses synthetic credentials and HTTP fixtures; it does not establish a
successful Santander login or complete financial extraction.

## Purpose and actors

The deployment owner enters credentials on their own computer, starts one Santander
synchronization, and compares the resulting products and movements with their bank
portal. The coding agent prepares and verifies the tools with synthetic data. The
owner performs the live login and financial comparison without submitting bank
credentials or financial reports to the chat.

This extends [the initial banking API](initial-api.md), especially OF-REQ-001,
OF-REQ-002 and OF-REQ-005. It does not add automatic login retries, challenge handling,
payment initiation, historical backfill, or a hosted credential collection service.

## Requirements and acceptance scenarios

### OF-REQ-006 — Private, resumable owner-operated live trial

The repository SHALL provide a local terminal flow that checks a live deployment,
collects credentials without terminal echo, records one synchronization attempt,
and produces a private comparison report plus a separately shareable status summary.
User-facing instructions and terminal messages SHALL be in Spanish. The tool SHALL
keep credentials out of command arguments, environment variables, shell history,
printed output and its own saved state. Credentials remain encrypted by the API's
existing connection lifecycle contract.

#### OF-AC-009 — Local entry and transport boundaries

Before prompting for bank credentials, the tool SHALL verify the API is healthy and
explicitly in live mode. Demo, malformed or unreachable health responses SHALL stop
the flow before the credential prompt or a connection/sync mutation. Entry SHALL
require an interactive terminal. RUT and password SHALL use hidden prompts; if
`getpass` cannot suppress echo, the tool SHALL fail before accepting credentials.
Noninteractive execution SHALL not fall back to reading credentials from stdin,
ordinary text input, command-line options or environment variables.

The API client SHALL construct its origin from `http://127.0.0.1` and a validated
local TCP port. It SHALL not accept arbitrary URLs. Requests SHALL disable environment
proxy discovery and SHALL refuse every HTTP redirect. Authorization or bank
credentials SHALL never be forwarded to a redirect target. Error handling SHALL use
fixed safe messages/codes; response bodies and raw exceptions SHALL not be printed.
Request timeouts SHALL bound each local HTTP call. The API bearer token SHALL be
read privately from the local deployment configuration or a hidden prompt and SHALL
never appear in the shareable summary.

Preparing the local deployment SHALL target this repository's explicit Compose
project and files, build the current source before starting the services, and switch
demo onboarding to live mode with a 600-second worker timeout. It SHALL preserve the
existing API token, encryption key, persistent database and sync cooldown. Repeating
preparation for the same live configuration SHALL preserve its contents. Configuration
files with unsafe permissions SHALL fail before invoking Docker. Compose arguments,
terminal output and errors SHALL not expose configuration secrets or raw subprocess
output.

#### OF-AC-010 — One bank attempt and explicit resumption

The tool SHALL create one live Santander connection and request at most one sync for
that attempt. It SHALL persist the connection identifier before enqueueing a job,
and persist the job identifier before polling. Polling SHALL use GET requests only.
A worker or bank failure, timeout, HTTP failure or terminal interruption SHALL not
cause an automatic second POST sync request, new connection or bank login.

Saved state SHALL bind the attempt to its local API origin and live connection/job
identifiers. Re-running the same attempt SHALL resume a recorded queued/running job
or inspect its terminal result without prompting for bank credentials or enqueueing
another job. If a mutation's outcome is uncertain, the tool SHALL stop with safe
recovery instructions rather than repeat that mutation. Starting another bank
attempt requires an explicit owner action and remains subject to the API cooldown.
The tool SHALL not automatically lower the cooldown or restart a failed bank session.

After a successful job, the client SHALL collect every page of accounts, cards and
movements for that exact connection, with a per-page limit of at most 200. It SHALL
validate page shape, offsets, limits and consistent totals, reject truncated or
inconsistent pagination, and fail rather than describe an incomplete page set as a
successful extraction. API cache completeness is separate from bank-history coverage.

#### OF-AC-011 — Private comparison and report

Local report/state artifacts SHALL reside under the ignored `data/` directory by
default, in an owner-only directory (0700) and owner-only files (0600). The tool SHALL
refuse unsafe filesystem targets instead of following symlinks or exposing financial
data through overly broad permissions. Its saved state SHALL contain identifiers
and attempt status only, without credentials, bearer tokens or financial records.

The financial comparison report SHALL be written locally, with accounts and cards
separated, currency and statement source preserved, and movement-level values
available for comparison. Monetary values SHALL retain decimal precision. Invalid
product references, currencies, monetary values or malformed API payloads SHALL
fail validation. A successful API job alone SHALL not mark the bank comparison as
verified. The report SHALL state known coverage limits and let the owner distinguish
confirmed products/samples from discrepancies and unreviewed data.

The owner compares product presence, CLP/USD, billed/unbilled sources, representative
dates/amounts and legitimate duplicate movements with Santander's own portal.
Discrepancies SHALL be described locally. An absent product or statement source
SHALL remain unverified unless the owner confirms it is absent in their bank view.
The coding agent SHALL not read the resulting private report, credentials, database,
deployment secrets or bank browser session as part of this trial.

#### OF-AC-012 — Minimal shareable result

The tool SHALL emit a separate structured status summary suitable for sharing in
chat. Its fields SHALL be explicitly allowlisted and limited to attempt outcome,
safe failure codes, aggregate counts, currency/source coverage, comparison status
and fixed coverage limitations. It SHALL omit RUT, password, API token, account/card
identifiers and last four digits, labels, descriptions, movement dates, amounts,
balances, raw API errors, browser content and private report contents. The summary
SHALL distinguish API extraction success from the owner's completed comparison.

Terminal output SHALL contain only Spanish guidance, safe status and paths to local
artifacts. It SHALL not print the private report or financial response body. An
unexpected exception SHALL be reduced to a fixed safe error instead of a traceback.

## Evidence and validation boundary

Tests use synthetic credentials, isolated temporary directories and loopback HTTP
servers. They SHALL not contact Santander, read a real deployment environment or
inspect the owner's local bank data. The initial API tests continue to establish
encryption, authentication, cooldown and worker behavior.

The first live result is owner-supplied evidence. Until the owner completes the local
comparison, Santander compatibility, product coverage, MFA behavior and movement
correctness remain unverified. A synthetic demonstration, green CI, or a running
Docker stack SHALL not be described as a successful live bank validation.

## Traceability

Parameterized node IDs below denote all cases belonging to the named function.

| Acceptance scenario | Exact executable test nodes | Evidence boundary |
| --- | --- | --- |
| OF-AC-009 | `tests/test_local_validation.py::test_redirects_never_forward_authorization_or_credential_body`<br>`tests/test_local_validation.py::test_environment_proxies_are_bypassed_for_private_requests`<br>`tests/test_local_validation.py::test_http_errors_do_not_echo_private_response_bodies`<br>`tests/test_local_validation.py::test_malformed_or_nonobject_json_fails_without_response_echo`<br>`tests/test_local_validation.py::test_api_transport_accepts_only_a_valid_local_port`<br>`tests/test_local_validation.py::test_noninteractive_terminal_is_rejected`<br>`tests/test_local_validation.py::test_getpass_echo_warning_fails_closed`<br>`tests/test_local_validation.py::test_live_health_is_checked_before_collecting_credentials_or_mutating` | Synthetic loopback HTTP, environment and terminal fixtures. No real credentials or bank connection. |
| OF-AC-010 | `tests/test_local_validation.py::test_all_pages_fetches_every_item_with_a_limit_of_200`<br>`tests/test_local_validation.py::test_inconsistent_or_truncated_pages_fail`<br>`tests/test_local_validation.py::test_oversized_collection_is_rejected_before_unbounded_pagination`<br>`tests/test_local_validation.py::test_one_sync_is_requested_and_rerun_reuses_the_saved_job`<br>`tests/test_local_validation.py::test_saved_attempt_cannot_resume_against_a_different_local_api_origin`<br>`tests/test_local_validation.py::test_interrupting_polling_resumes_without_another_bank_attempt`<br>`tests/test_local_validation.py::test_uncertain_mutation_outcome_is_never_repeated_automatically`<br>`tests/test_local_validation.py::test_failed_job_does_not_retry_login_or_disclose_bank_error` | Synthetic API pagination, durable identifiers and interruptions. No claim of bank retry safety beyond one client attempt and the existing worker contract. |
| OF-AC-011 | `tests/test_local_validation.py::test_local_artifacts_are_private_and_unsafe_links_are_refused`<br>`tests/test_validation_report.py::test_filters_cached_rows_and_does_not_claim_their_products_or_movements`<br>`tests/test_validation_report.py::test_wrong_job_connection_fails_without_selecting_rows`<br>`tests/test_validation_report.py::test_invalid_or_foreign_snapshot_rows_fail`<br>`tests/test_validation_report.py::test_legitimate_duplicate_rows_with_distinct_ids_are_preserved`<br>`tests/test_validation_report.py::test_invalid_movement_identity_reference_source_date_or_amount_fails`<br>`tests/test_validation_report.py::test_invalid_product_identity_kind_currency_or_balance_fails`<br>`tests/test_validation_report.py::test_empty_products_fail_instead_of_looking_like_a_successful_empty_bank`<br>`tests/test_validation_report.py::test_absent_sources_warn_and_complete_history_is_never_inferred`<br>`tests/test_validation_report.py::test_report_escapes_bank_text_and_never_copies_unselected_fields_or_raw_warnings`<br>`tests/test_validation_report.py::test_decimal_amount_is_not_rounded`<br>`tests/test_validation_report.py::test_owner_review_status_is_separate_from_structure`<br>`tests/test_validation_report.py::test_owner_confirmation_does_not_override_structural_failure` | Isolated temporary files and synthetic financial fixtures. Owner comparison with the real portal remains manual and unverified until performed. |
| OF-AC-012 | `tests/test_local_validation.py::test_shareable_status_and_terminal_output_omit_private_values`<br>`tests/test_local_validation.py::test_unrecognized_api_error_codes_are_not_copied_to_shareable_status`<br>`tests/test_local_validation.py::test_cli_unexpected_error_is_safe_without_a_traceback`<br>`tests/test_validation_report.py::test_counts_are_safe_and_distinguish_physical_cards_from_currency_products`<br>`tests/test_validation_report.py::test_selected_rows_contain_only_documented_fields_and_do_not_mutate_input` | Synthetic positive privacy assertions. No private financial artifact is uploaded or read from a live deployment. |

OF-AC-009 also has explicit authentication-before-prompt evidence in
`tests/test_local_validation.py::test_api_authentication_is_checked_before_collecting_bank_credentials`.
Preparation is covered by
`tests/test_local_validation.py::test_prepare_live_preserves_secrets_and_cooldown_and_builds_explicit_project`,
`tests/test_local_validation.py::test_prepare_live_is_idempotent_for_an_existing_live_configuration`,
and `tests/test_local_validation.py::test_prepare_live_rejects_an_environment_file_with_public_permissions`.
OF-AC-012 subprocess redaction evidence is
`tests/test_local_validation.py::test_prepare_live_redacts_compose_failures`.
OF-AC-011 also covers static report contents and early discrepancy status in
`tests/test_validation_report.py::test_static_report_contains_all_products_movements_and_manual_review_questions`
and `tests/test_validation_report.py::test_structural_failure_is_a_discrepancy_even_before_manual_review`.
