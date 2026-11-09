# Slimes.Hustlers Marketing Agent

A single-user campaign planning and content drafting application for
Slimes.Hustlers AI Solutions. The application stores campaign/post data in
SQLite and keeps an explicit human review step before any post is approved.
Approval gates content for an explicit second publishing action. Facebook
publishing is opt-in and requires server credentials; LinkedIn drafting remains
draft-only.

## Local setup

Requirements: Python, Node.js, and npm. A Gemini API key is optional; without
it the app uses local templates.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Set `APP_ENV=development` and `MARKETING_AUTH_SECRET` in `.env`. The secret
must be private and randomly generated, with at least 32 bytes. For example,
generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Do not commit `.env` or expose its values through frontend `VITE_*` variables.
Set `MARKETING_AI_ENABLED=true` and provide `GEMINI_API_KEY` only if Gemini
drafting is wanted.

The API reads configuration from process environment variables; it does not
load `.env` automatically. Load the private root `.env` file into the current
PowerShell process before starting the API:

```powershell
Get-Content .\.env | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)\s*$') {
        $name = $Matches[1]
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}
```

The snippet sets environment variables in memory without printing secret
values. Repeat this loading step in each new terminal that starts the API.

Run the API:

```powershell
python -m uvicorn src.api.main:app --reload --port 8765
```

In another terminal:

```powershell
Set-Location frontend
npm ci
Copy-Item .env.example .env.local
npm run dev
```

The development frontend uses `http://127.0.0.1:8765` by default. Open the URL
printed by Vite and enter the server's `MARKETING_AUTH_SECRET` in the login
screen. The token is held in JavaScript memory only; reloading the page signs
you out. `/health` is public; all campaign and post routes require the bearer
token. API docs at `/docs` describe protected operations but cannot execute
them without authorization.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `MARKETING_AUTH_SECRET` | Yes | Server-only bearer token; at least 32 UTF-8 bytes. No default is provided. |
| `APP_ENV` | Yes | Must be explicitly `development` or `production`; the API refuses to import or start otherwise. |
| `CORS_ALLOWED_ORIGINS` | Production | Comma-separated exact browser origins; required in production, wildcard is rejected. |
| `MARKETING_DB_PATH` | Production | SQLite file path on a persistent mounted volume. |
| `MARKETING_AI_ENABLED` | No | Enables Gemini generation when true; defaults to false. |
| `GEMINI_API_KEY` | If AI enabled | Server-side Gemini credential. Keep it out of frontend variables. |
| `GEMINI_MODEL` | No | Gemini model name. |
| `GEMINI_TIMEOUT_MS` | No | Per-generation network timeout, bounded to 1,000–60,000 ms; default 20,000. |
| `FACEBOOK_PAGE_ID` | Facebook publishing | Numeric Meta Page ID used for explicit publishing. |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | Facebook publishing | Server-side Page access token with permission to publish feed posts. Never place this in a `VITE_*` variable. |
| `FACEBOOK_PUBLISH_TIMEOUT_MS` | No | Per-publish network timeout, bounded to 1,000–60,000 ms; default 20,000. |
| `FACEBOOK_GRAPH_API_BASE` | No | HTTPS Graph API base URL; defaults to the current `https://graph.facebook.com/v26.0`. Must use the `graph.facebook.com` host and a `vN` or `vN.N` path. |
| `VITE_API_BASE_URL` | Production frontend build | Public API URL compiled into the frontend; production build requires a valid absolute HTTPS URL and rejects localhost/loopback. Never place credentials or secrets in any `VITE_*` variable. |

### Facebook publishing

Facebook publishing is deliberately opt-in. Creating a Facebook draft and
explicitly selecting **Approve & publish** approves it and then immediately
publishes it through a separate server-side publishing request. LinkedIn
drafts remain approval-only. The server posts the exact approved content to
the configured Page through
Meta's Graph API and stores the returned external post ID to prevent duplicate
publishing.

A confirmed HTTP 4xx rejection may be retried after the cause is corrected,
provided the backend permits it. Timeouts, network failures, HTTP 5xx
responses, invalid responses, and database-save failures can leave the outcome
uncertain. For such outcomes, check the Facebook Page and reconcile the database
state before retrying; never assume that an uncertain outcome failed or
succeeded. Configure `FACEBOOK_PAGE_ID` and `FACEBOOK_PAGE_ACCESS_TOKEN` only
on the server. Use a Page access token for the target Page. Meta requires the
user/Page relationship to have `CREATE_CONTENT` task access and the app grant
for `pages_manage_posts` (plus `pages_manage_metadata`,
`pages_manage_read_engagement`, and `pages_show_list` for discovery as described
by Meta). Obtain the Page ID and token from `/me/accounts`; do not rely on a
personal-profile token. Meta permissions require App Review when the app is live,
and development-mode access is limited to app roles. Rotate or revoke the token
using Meta when no longer needed.

#### Recovering an interrupted Facebook publish

The API does not automatically recover `FACEBOOK_PUBLISH_IN_PROGRESS` claims.
An interrupted request may have reached Facebook even if the local process
never received or saved the response. Do not clear a claim based only on its
age, restart the publish request, or assume that a missing post in a quick Page
check proves that Facebook did not publish it.

For a claim that remains in progress after the publishing process has stopped:

1. Stop the API and confirm no API worker is still running. Do not perform
   recovery while the database is being used.
2. Make and verify a separate, access-controlled copy of the SQLite database
   before inspecting or changing it. Keep the original unchanged as a recovery
   copy.
3. Inspect the single affected row by `post_id`, confirming its campaign,
   platform, approval status, content, external post ID, publication timestamp,
   and `publication_error`. The expected state is an approved Facebook post,
   no external ID, and `FACEBOOK_PUBLISH_IN_PROGRESS`.
4. Manually check the configured Facebook Page for a post matching the exact
   approved content and the relevant time. Record the operator, time, post ID,
   evidence checked, and decision in the pilot's operational incident record;
   the application does not keep per-user recovery audit history.
5. If the matching post is found, record its exact Facebook post ID and
   confirmed publication time in the database, and clear the in-progress
   marker in one transaction. Restrict the update to the exact `post_id` and
   expected in-progress state, and verify exactly one row changed. Do not
   replace an existing external ID or alter the approved content.
6. If non-publication is conclusively established through authoritative
   verification, clear the in-progress marker for that exact row in one
   transaction, only while its external ID is still empty and its state is
   still `FACEBOOK_PUBLISH_IN_PROGRESS`. Verify exactly one row changed. A
   missing result in a limited or delayed Page view is not conclusive evidence.
   Only after this documented human decision may an operator use the normal
   explicit publishing action once.
7. If neither publication nor non-publication can be established, leave the
   row unchanged and blocked. Escalate for further verification; never clear
   the marker or retry to see what happens.
8. Restart the API only after the database change has been checked and
   committed. Read the post through the authenticated API and confirm its
   external ID and state. Do not issue a publish request for a row with an
   external ID or an unresolved outcome.

This is a manual database recovery procedure, not an application feature. Use
an SQLite client and transaction handling appropriate to the deployment, and
preserve the pre-recovery copy. If the database cannot be safely edited or the
verification is uncertain, keep publishing disabled for that post and obtain
assistance rather than retrying.

The Vite development default is `http://127.0.0.1:8765`. `frontend/.env.example`
shows the local value. For a production frontend build, set `VITE_API_BASE_URL`
to the HTTPS API URL in the build environment. The build fails if the variable
is missing, malformed, non-HTTPS, localhost/loopback, or if secret-like Vite
variable names are present.

## Production deployment requirements

- Terminate TLS at the ingress, reverse proxy, or hosting platform and redirect
  HTTP to HTTPS there. The proxy-to-API network should also be private or
  encrypted. The API does not infer external HTTPS from proxy headers.
- Supply `APP_ENV=production`, a unique high-entropy randomly generated
  `MARKETING_AUTH_SECRET` (at least 32 bytes), exact `CORS_ALLOWED_ORIGINS`,
  and `MARKETING_DB_PATH` through the host's secret/configuration facilities.
  Never put server credentials in frontend build variables.
- `MARKETING_DB_PATH` must be absolute, and its parent directory must already
  exist and be writable at startup. The application cannot verify that the host
  has mounted durable storage there; verify the volume mapping yourself. The
  service initializes missing tables; `CREATE TABLE IF NOT EXISTS`
  preserves an existing database. Ensure the process user can write to the
  volume and test persistence across a restart before the pilot.
- Run exactly one API instance with one Uvicorn worker against a SQLite
  database. The Gemini concurrency limit is two calls per Python process.
  Do not share this file across concurrent replicas or network filesystems.
  For horizontal scaling, migrate to a server database first.
- Place request rate limiting and a request-body limit at the ingress. The
  application rejects requests declaring bodies over 1 MiB; the ingress must
  also cap streamed/chunked bodies; no ingress configuration is included here,
  so deployment enforcement must be verified separately. Gemini generation
  uses a 20-second default timeout, at most two concurrent calls per process, and falls back to templates
  when capacity or the provider is unavailable.
- The shared bearer token is a simple single-user pilot credential: there are
  no separate accounts, token rotation endpoint, per-user audit identities,
  or brute-force rate limiter in the application. Restrict API network access
  and apply rate limiting at the ingress. Rotate by updating the server secret
  and signing in again.

### SQLite backup and restore

The application does not configure SQLite's journal mode; verify the mode used
by the deployed database rather than assuming it. When SQLite uses write-ahead
logging (WAL), committed changes may still be in the `-wal` sidecar, while
`-shm` supports coordination between database connections. Copying only the
main `.db` file may therefore omit committed changes. Do not copy a live
database file as a backup.

Prefer SQLite's online backup API to create a consistent snapshot while the
database is open. Store backups in encrypted, access-controlled storage, and
restrict access because they contain campaign and post data. Validate each
backup before relying on it:

1. Run `PRAGMA integrity_check` and require exactly `ok`.
2. Run `PRAGMA foreign_key_check` and require zero rows.
3. Verify that the expected application tables, indexes, constraints, and
   campaign, post, and review-audit data are present.
4. Record the backup time and validation result without exposing database
   contents in logs or reports.

For an offline restore or file replacement, stop all API instances and workers
and confirm that no other process has an open connection. Preserve the current
database as a separate rollback copy before proceeding. Restore the selected
backup into a new temporary database on the same volume as `MARKETING_DB_PATH`;
do not overwrite the active database during restore. Close all SQLite
connections, validate the staged database using the checks above, and confirm
the expected application schema and data before replacing the configured
database.

Before replacement, account for SQLite sidecars. After a clean shutdown, verify
that no stale `-wal` or `-shm` files can be applied to the replacement. Do not
delete sidecars while a process may have the database open; if their state is
unclear, stop and obtain SQLite-aware assistance. Replace the active file only
after validation succeeds and all connections are closed, while preserving the
rollback copy. If any step fails, do not activate the staged file; retain the
current database and investigate.

After replacement, verify file ownership and permissions, then start only the
intended API instance and worker configuration. Check `/health` and perform
authenticated campaign and post reads. If startup or application checks fail,
stop all API workers and restore the preserved rollback copy using the same
staged and validated process.

Production-specific journal mode, API replica/worker count, persistent storage
mapping, backup retention, encryption controls, recovery point objective
(RPO), and recovery time objective (RTO) must be verified or agreed for the
deployment; this repository does not establish those operational facts or
guarantees. Do not restore or replace the database while any API connection
may remain open.

## Checks

From the repository root:

```powershell
python -m pytest -q
```

From `frontend`:

```powershell
npm run test
npm run test:component
npm run lint
$env:VITE_API_BASE_URL = 'https://api.example.invalid'
npm run build
```

`example.invalid` is a documentation-only domain; set the real API URL in the
deployment build environment.
