# Slimes.Hustlers Marketing Agent

A local-first marketing campaign and content drafting application for
Slimes.Hustlers AI Solutions, based in Maseru, Lesotho.

## Features

- Create, list, and retrieve marketing campaigns.
- Generate LinkedIn and Facebook draft posts using the Gemini API when enabled.
- Fall back to local templates when AI generation is disabled, the API key
  is missing, or Gemini generation fails.
- Persist campaigns and posts in SQLite.
- Require explicit human approval or rejection before a draft is considered
  approved.
- Prevent duplicate draft creation for the same campaign.
- Record the draft generation source and review status.
- Provide a React frontend and FastAPI backend.

**Publishing limitation:** This version does not publish posts automatically
to LinkedIn or Facebook. Approval records a review decision; it does not
publish the content.

## Requirements

- Python compatible with the installed project dependencies.
- Node.js and npm.
- A Gemini API key for AI-powered draft generation.

## Backend setup

Create and activate a virtual environment if you do not already have one:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the runtime and development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Create your local environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and configure:

- `MARKETING_AI_ENABLED=true` to enable AI generation.
- `GEMINI_MODEL=gemini-flash-lite-latest` to select the configured model.
- `GEMINI_API_KEY` with your own Gemini API key.

Keep `.env` private. Never commit API keys.

Start the backend on port 8765, which matches the frontend API address:

```powershell
python -m uvicorn src.api.main:app --reload --port 8765
```

API documentation: http://127.0.0.1:8765/docs

Health check: http://127.0.0.1:8765/health

## Frontend setup

Open a second terminal:

```powershell
Set-Location frontend
npm ci
npm run dev
```

Open the local URL printed by Vite, usually http://localhost:5173.

The frontend currently targets `http://127.0.0.1:8765`. If the backend
address changes, update the frontend configuration accordingly.

## Testing

Run backend tests from the project root:

```powershell
python -m pytest -q
```

Run frontend lint and the production build:

```powershell
Set-Location frontend
npm run lint
npm run build
Set-Location ..
```

## Configuration and data

- `.env` contains local configuration and secrets.
- `data/marketing.db` is the default SQLite database.
- `MARKETING_DB_PATH` can override the database location.
- `.env.example` documents the expected environment variables.

The local database and generated frontend build output should not be committed.

## Current limitations

- No automatic social media publishing.
- No production social account authentication or publishing integration.
- Human review is part of the local workflow; deployment security,
  authentication, and authorization need separate production review.
- AI generation depends on Gemini availability and valid configuration.
