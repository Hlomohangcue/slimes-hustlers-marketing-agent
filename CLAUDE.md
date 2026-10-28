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
<!-- cloude-code-toolbox:mcp-skills-awareness-begin -->

### MCP & Skills awareness (Claude Code ToolBox)

_Last synced: 2026-10-24T11:31:38.632Z._

- **Full report:** `.claude/cloude-code-toolbox-mcp-skills-awareness.md` in this workspace (auto-overwritten on each scan). Use it as ground truth for configured servers and skill folders.
- **MCP:** For **live tools** in Claude Code, enable the matching server via `/mcp`. Servers are configured in `~/.claude.json` (user) and `.mcp.json` (project).
- **When the user’s task matches a server** (e.g. Confluence work and a **Confluence** / **Atlassian** MCP is listed), **prefer that server id** and plan on tool use—not only file search.
- **Skills:** Folders below contain `SKILL.md`; attach or cite paths in chat when relevant.

#### Workspace MCP

- `c:\Users\User\adk-workspace\slimes-hustlers-marketing-agent\.mcp.json` _(workspace: slimes-hustlers-marketing-agent)_ — _file missing_

_No active workspace servers in mcp.json._

#### User MCP

- `C:\Users\User\.claude.json` — _no servers defined_

_No active user-scoped servers in mcp.json._

#### Project skills

_None found (or no workspace open)._

#### User skills

- **agents-sdk** — `C:\Users\User\.copilot\skills\agents-sdk` — Build, debug, or review Cloudflare Agents SDK applications using the agents package.

- **basin** — `C:\Users\User\.copilot\skills\basin` — Build and troubleshoot Cloudflare Basin analytics workflows with Basin Pipelines, Basin Catalog, and Basin SQL. Use for streaming data into R2 Iceberg tables, managing catalogs, or querying those tables; also use for req

- **cloudflare** — `C:\Users\User\.copilot\skills\cloudflare` — Discover and choose Cloudflare products for apps, APIs, AI agents, storage, networking, and security. Use for architecture and product selection, including when the user describes a need without naming a Cloudflare produ

- **cloudflare-email-service** — `C:\Users\User\.copilot\skills\cloudflare-email-service` — Implement or troubleshoot Cloudflare Email Sending and Email Routing integrations and their delivery configuration.

- **cloudflare-one** — `C:\Users\User\.copilot\skills\cloudflare-one` — Design, configure, troubleshoot, or review Cloudflare One Zero Trust and SASE deployments. Use cloudflare-one-migrations for migration planning from other vendors.

- **cloudflare-one-migrations** — `C:\Users\User\.copilot\skills\cloudflare-one-migrations` — Assess and plan migrations from existing VPN, SWG, or SASE platforms to Cloudflare One, including policy mapping, parity gaps, and rollout.

- **durable-objects** — `C:\Users\User\.copilot\skills\durable-objects` — Build, debug, or review Cloudflare Durable Objects code for persistent state and coordination.

- **k2** — `C:\Users\User\.copilot\skills\k2` — Build and troubleshoot Cloudflare K2 or K2 Streams durable logs. Use for stream setup, producing from Workers or HTTP, configuring retention and inputs, and consuming through subscriptions.

- **nextjs-on-cloudflare** — `C:\Users\User\.copilot\skills\nextjs-on-cloudflare` — Build, migrate, and deploy Next.js apps on Cloudflare Workers with vinext. Use when starting a Next.js project on Cloudflare, moving an existing app to Workers, choosing between vinext and OpenNext, or setting up vinext

- **sandbox-migrate-to-next** — `C:\Users\User\.copilot\skills\sandbox-migrate-to-next` — Migrate Cloudflare Sandbox apps from stable @cloudflare/sandbox to @cloudflare/sandbox@next (SDK 1.0 preview). Use sandbox-next for apps already on the preview.

- **sandbox-next** — `C:\Users\User\.copilot\skills\sandbox-next` — Build or maintain Cloudflare Sandbox apps on @cloudflare/sandbox@next (SDK 1.0 preview). Use sandbox-migrate-to-next when porting a stable app.

- **sandbox-sdk** — `C:\Users\User\.copilot\skills\sandbox-sdk` — Build sandboxed applications for secure code execution. Load when building AI code execution, code interpreters, CI/CD systems, interactive dev environments, or executing untrusted code. Covers Sandbox SDK lifecycle, com

- **sandbox-stable** — `C:\Users\User\.copilot\skills\sandbox-stable` — Build or maintain Cloudflare Sandbox apps on the stable @cloudflare/sandbox package. Use sandbox-next for preview apps and sandbox-migrate-to-next for stable-to-preview migrations.

- **turnstile-spin** — `C:\Users\User\.copilot\skills\turnstile-spin` — Set up, repair, or migrate to Cloudflare Turnstile bot verification in an existing frontend and backend, including server-side Siteverify.

- **web-perf** — `C:\Users\User\.copilot\skills\web-perf` — Audit, diagnose, or optimize website loading and interaction performance, Core Web Vitals, and Lighthouse performance scores.

- **workers-best-practices** — `C:\Users\User\.copilot\skills\workers-best-practices` — Cloudflare Workers best practices for production applications. Use when writing, reviewing, or configuring Workers.

- **wrangler** — `C:\Users\User\.copilot\skills\wrangler` — Run or troubleshoot Wrangler CLI commands and configure Worker projects for local development, Previews, deployment, and Cloudflare resource management.

<!-- cloude-code-toolbox:mcp-skills-awareness-end -->
