# Security policy and deployment boundary

This is an early, single-owner self-hosted service. Live Santander integration is
unverified. One deployment must serve one owner connecting only their own products.
It is not a hardened multi-tenant hosting platform.

## Supported versions

Only the current development branch receives fixes. There are no supported stable
releases or response-time commitments yet.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository if enabled:
https://github.com/robertomontegu/open-fin/security/advisories/new

If private reporting is unavailable, open a minimal issue asking for a private
contact. Do not post an exploit, credentials, tokens, account numbers, financial
history, database backups, browser traces, or screenshots in public. Never send
real bank credentials to the maintainers.

## Controls in this version

- Banking endpoints and documentation require one deployment bearer token.
- Bank credentials are encrypted with a locally supplied Fernet key. Credentials
  are excluded from API responses, validation messages and application logs.
- The API queues durable work; one worker executes bounded jobs without automatic
  bank-login retries. The default login cooldown is one hour.
- Demo mode accepts only documented synthetic credentials and never contacts banks.
  Live mode does not fall back to demo data.
- Containers run as UID/GID `10001`. Compose binds the API to loopback, uses a
  read-only root filesystem, drops capabilities, and enables no-new-privileges.
- Chromium uses its sandbox; the worker has an adapted Playwright seccomp profile
  permitting user namespace syscalls and sandbox `chroot`. No privileged mode or sandbox
  disable option is provided. The host must also permit unprivileged user namespaces.
- Browser sessions are closed and browser profiles/screenshots are not persisted.
- Live extraction checks process-control support before starting a browser. The
  adapter inherits the worker lock, and bounded cleanup includes browser processes
  that start separate sessions. Unsupported hosts fail before bank login.

## Operator responsibilities and limits

The bearer token grants full access to the deployment, including deletion of data
and scheduling bank logins. Keep it and the encryption key private. Generated `.env`
files have mode `0600` and are excluded from Git and Docker build contexts. Keep
secrets out of diagnostics and shell history. Docker engine and host administrators
can inspect container environment variables and volumes; they are trusted operators.

Bank credentials are encrypted at rest, but cached financial records are not
application-encrypted. Use host/volume encryption, restricted access and protected
backups. Back up the key with the database. Key rotation and secret recovery are not
implemented. Treat a host compromise as a compromise of the deployment and its key.

The default HTTP listener is local only. Remote access requires an SSH tunnel or an
operator-managed HTTPS reverse proxy and access controls. There is no public-facing
abuse prevention, multi-user authorization, tenant isolation or hosted onboarding.
Run exactly one worker. SQLite storage does not provide a horizontally scaled service.

Financial parsing and fixtures reduce errors but cannot establish current bank
compatibility, all MFA paths, complete history or financial correctness for every
product. Validate against your own bank records before relying on extracted data.
Avoid repeated failed logins; bank policies may block accounts. Do not reduce the
cooldown casually. Dependency updates and host sandbox support remain ongoing
maintenance responsibilities.
