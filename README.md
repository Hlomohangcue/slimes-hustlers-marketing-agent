# Slimes.Hustlers Marketing Agent

A single-user campaign planning and content drafting application for
Slimes.Hustlers AI Solutions. The application stores campaign/post data in
SQLite and keeps an explicit human review step before any post is approved.
Approval does not publish content, and this version has no social publishing
integration.

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
Do not commit `.env` or put this value in any `VITE_*` variable. Set
`MARKETING_AI_ENABLED=true` and provide `GEMINI_API_KEY` only if Gemini drafting
is wanted.

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
| `VITE_API_BASE_URL` | Production frontend build | Public API URL compiled into the frontend; production build requires a valid absolute HTTPS URL and rejects localhost/loopback. Never place credentials or secrets in any `VITE_*` variable. |

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

Stop the API before making a file copy so SQLite has no active writes. Copy the
database file from the mounted volume to encrypted, access-controlled backup
storage. To restore, stop the API, preserve the current database as a separate
recovery copy, put the selected backup at `MARKETING_DB_PATH`, verify file
ownership/permissions, then start the API and check `/health` and authenticated
campaign/post reads. Retain backups according to the pilot's data-retention
policy. Do not restore over a running database.

## Checks

From the repository root:

```powershell
python -m pytest -q
```

From `frontend`:

```powershell
npm run lint
$env:VITE_API_BASE_URL = 'https://api.example.invalid'
npm run build
```

`example.invalid` is a documentation-only domain; set the real API URL in the
deployment build environment.
