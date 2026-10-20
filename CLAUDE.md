# Slimes.Hustlers Marketing Agent

A local-first marketing campaign and content drafting application for Slimes.Hustlers AI Solutions.

## Architecture
- Backend: Python, FastAPI, SQLite, and Google Gemini.
- Frontend: React and Vite.
- Tests: pytest, oxlint, and Vite production build.

## Development checks
Run python -m pytest -q from the project root.
Run npm run lint and npm run build from the frontend directory.

## Safety requirements
- Never commit secrets or real API keys.
- Keep .env.example free of real credentials.
- Require human approval before content is considered approved.
- Do not automatically publish social media content without explicit requirements and authorization.
- Add tests for behavior changes and review staged changes before committing.

## Configuration
See README.md and .env.example for setup instructions.
