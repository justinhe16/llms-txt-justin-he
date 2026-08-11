# llms-text

Generate and maintain [`llms.txt`](https://llmstxt.org) files for websites. A signed-in user
registers a **website**, the backend crawls it as a **run**, and each run produces an `llms.txt`
artifact. Runs are kept, so a website accumulates history.

| | |
| --- | --- |
| **Live app** | **https://llms-text-justin-he-gamma.vercel.app** |
| API | https://llms-text-justin-he.fly.dev · [`/docs`](https://llms-text-justin-he.fly.dev/docs) · [`/health`](https://llms-text-justin-he.fly.dev/health) |
| Source | https://github.com/justinhe16/llms-txt-justin-he |

Next.js App Router on Vercel · FastAPI + an ARQ worker on Fly.io (one image, two processes) ·
Supabase for Postgres, Auth and Storage · Prisma for schema and migrations only, asyncpg at
runtime. The Fly app and Vercel project keep their original `llms-text` hostnames.

**This file is the quickstart: how to run the thing locally, and how to work on it.** Everything
else — architecture, the authorization contract, deploy policy, infrastructure, secrets, and how
to enable publishing to GitHub — lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md), which is the
engineering contract and wins wherever the two disagree.
[`CLAUDE.md`](./CLAUDE.md) is the short list of rules that are expensive to get wrong.
[Where to find what](#where-to-find-what) at the bottom maps the rest.

---

## Screenshots

| | |
| --- | --- |
| ![Landing page](./docs/screenshots/01-landing.png) | ![The crawls table](./docs/screenshots/02-crawls.png) |
| Paste a URL and the first run starts. | Every registered site, with its latest run. |
| ![The Output tab](./docs/screenshots/03-output.png) | ![The Trends tab](./docs/screenshots/04-trends.png) |
| The generated `llms.txt`, with `llms-full.txt` beside it. | What changed between runs, and how each run was built. |

---

## Run locally

### Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Python | 3.12+ | `backend/` API and worker |
| Node | 20.19+, 22.13+, or 24+ | `frontend/`, and the Prisma CLI in `db/` |
| Docker | current, and **running** | Postgres, Auth, Storage, Redis |
| Supabase CLI | **2.111.0, pinned** | the local Supabase stack |
| Fly CLI (`flyctl`) | current | inspecting deploys, setting secrets |

Install the pinned Supabase CLI exactly — `supabase/config.toml` was verified against 2.111.0 and
config keys move between releases. `brew install supabase/tap/supabase` or
`npm install -g supabase@2.111.0`; confirm with `supabase --version`.

`python3` itself need not be 3.12. The `Makefile` takes the first of `python3.12`, `python3.13`,
`python3.14`, `python3` that satisfies `backend/pyproject.toml`. Override with
`make setup PYTHON=/path/to/python3.13`.

### Setup

```bash
make setup            # once per checkout: backend/.venv, plus backend, db and frontend deps
supabase start        # Postgres, Auth, Storage — migrations need a live database
make migrate-apply    # apply db/migrations/ to the local database
make dev              # Supabase, Redis, the API, the worker, the frontend
```

Re-run `make setup` after a dependency change. Everything after it is idempotent.

After the first time, `make dev` is the only command you need — it starts Supabase itself.
`supabase start` is listed separately because `make migrate-apply` reads its connection string
from a **running** stack. Prisma owns the schema
([§6](./ARCHITECTURE.md#6-database-and-migration-policy)) and `supabase/migrations/` is
deliberately empty, so a database that has never seen `make migrate-apply` has no application
tables.

**There is no `.env` to fill in.** `make dev` derives `DATABASE_URL`, `SUPABASE_URL`,
`SUPABASE_SECRET_KEY` and `REDIS_URL` from the running stack (`scripts/local-env.sh`) and writes
`frontend/.env.local`. The `.env.example` files document every variable and are the reference for
deployed environments; copy one to `backend/.env` only to override a default such as
`LOG_LEVEL=DEBUG`.

### Verify

`make dev` prints this, then interleaved `[api]`, `[worker]` and `[frontend]` lines. All three
matter.

```
llms-text dev environment is up:
  App              http://localhost:3000
  API              http://127.0.0.1:8000
  API docs         http://127.0.0.1:8000/docs
  Supabase Studio  http://localhost:54323
```

Ports open before the stack behind them works, so check the body, not the status code:

```bash
curl -s 127.0.0.1:8000/health     # {"status":"ok","db":"ok","redis":"ok"}
```

`make dev` fails loudly if `arq` is missing rather than skipping the worker: a dev environment
with no queue consumer looks exactly like one where nothing is happening. `make setup` refreshes
the virtualenv.

`Ctrl-C` stops the API, worker and frontend. Supabase and Redis keep running in Docker, so local
data survives — `make down` stops those too.

### First run

1. Open http://localhost:3000, sign in as the test user below.
2. Paste a URL. The first run starts immediately.
3. Watch `[worker]` for `crawl: completed`, then read the **Output** tab.

A small site takes a couple of seconds.

**Local state belongs to the Supabase project, not to your checkout.** `supabase/config.toml`
pins `project_id = "llms-text-justin-he"`, and the CLI keys its containers on that — so two
clones of this repo on one machine share one stack and one database, and a freshly cloned
checkout can open on websites another checkout registered. That is the CLI working as intended,
not a stale-state bug. `make reset` gives you an empty database and the seed user back.

### Local test user

Seeded by `supabase/seed.sql` on first database init, and on every `make reset`.

| Field | Value |
| --- | --- |
| Email | `dev@llms-text.test` |
| Password | `devpassword123` |

Use the email/password form on `/`, which renders in development builds only. The password
unlocks a Postgres container on your own machine and nothing reachable from outside it. GitHub
OAuth is verified in production but deliberately disabled locally (`supabase/config.toml`).

### Everyday commands

`make help` prints this list and is the source of truth for it.

```bash
make help          # show every target with its one-line purpose
make setup         # create backend/.venv and install backend + db deps (frontend if present)
make dev           # run Supabase, Redis, the API, worker, and frontend
make migrate       # create a migration from schema.prisma (review the SQL, then commit it)
make migrate-apply # apply pending migrations to the local database
make test          # run the backend and frontend test suites
make lint          # ruff + mypy on backend/, eslint + tsc on frontend/, OpenAPI drift checks
make openapi       # regenerate the OpenAPI snapshot and the generated TS client types
make down          # stop Supabase and Redis containers
make reset         # recreate the local DB, reseed it, and replay Prisma migrations
```

`make test` runs the backend suite either way; its database-backed tests skip with a message when
the local stack is down.

### Troubleshooting

| Symptom | Cause, and what to do |
| --- | --- |
| `make: tried python3.12, python3.13, python3.14, python3.` | None satisfies `requires-python`. Install 3.12+, or `make setup PYTHON=/path/to/python3.13`. |
| `npm warn EBADENGINE` during `make setup` | Node is outside a dev dependency's declared range — Node 23 is the common case. Warning only. |
| `npm audit` reports high/critical during `make setup` | Mixed, and worth knowing which is which. The **critical** (`vitest`) and one high (`js-yaml`, via `@redocly`) are dev-only. The remaining highs are **not**: `sharp` and `postcss` reach production through `next`, which is a runtime dependency. Neither blocks local development, and the only fix npm offers is a Next 16 major bump — deferred, and recorded in [§11](./ARCHITECTURE.md#11-out-of-scope). Setup still succeeded. |
| A port is already in use | Supabase 54320–54324, Redis 6379, API 8000, frontend 3000. Find it with `lsof -nP -iTCP:<port> -sTCP:LISTEN`. `API_PORT=8001 make dev` moves the API for the session (`API_HOST` likewise); change Supabase ports in `supabase/config.toml`, not on the command line. |
| Docker isn't running | Every target that needs it checks for a live daemon — not just the binary — and says so first. |
| A stale container | `make down` is the clean shutdown. If still stuck: `docker ps -a`, `docker rm -f <name>`, `make dev`. |
| The database is in an unknown state | `make reset` recreates, reseeds, and replays every migration. |
| A run sits at `pending` forever | Nothing is consuming the queue. No `ARQ worker ready` in `[worker]` means the worker died at startup; confirm Redis with `curl -s 127.0.0.1:8000/health`. |
| A fresh clone already has websites in it | Expected — see [First run](#first-run). Local state is keyed on `project_id`, not on the directory. `make reset` clears it. |
| The Supabase client throws on first use | You ran `npm --prefix frontend run dev` directly. Run `scripts/local-env.sh write-frontend-env` first, or use `make dev`. |
| Next.js: `inferred your workspace root` | A stray `package-lock.json` above the repo, often `~/package-lock.json`. Harmless — delete it or set `outputFileTracingRoot`. |
| `supabase start` prints a wall of JSON | Non-TTY output. Expected; those are the well-known local demo keys, not secrets. |

---

## CI

Two workflows in [`.github/workflows/`](./.github/workflows), one per stack: `ci-backend.yml` for
`backend/**` and `db/**`, `ci-frontend.yml` for `frontend/**`. Where they overlap with the
`Makefile` they run the same `make lint` and `make test` — change how a check is invoked in one
and change the other in the same pull request, or a green laptop stops meaning a green PR.

**The required checks on `main` are the `backend-ci` and `frontend-ci` gate jobs, not the jobs
that do the work.** Both workflows start on every PR and the path filter lives in a `changes` job,
so expensive jobs are *skipped* rather than never started. A `paths:` trigger would stop the
workflow from starting; a workflow that never starts never reports; a required check that never
reports blocks the merge forever — making every docs-only PR unmergeable. The filter is
[`changed-paths.sh`](./.github/scripts/changed-paths.sh), and it has its own test that both
workflows run before trusting it.

`main` requires linear history: merge with `gh pr merge <n> --squash`, never a merge commit.

The frontend is checked at three levels, because each passes on failures the others catch. `tsc`
and eslint prove types and style line up. **Vitest** covers pure logic under `frontend/lib/` in a
node environment with no jsdom and no React, so it stays a unit suite. A **rendered-output smoke
test** loads the built page in headless Chrome and measures computed styles, because `tsc`, eslint
and `next build` all pass on a page that renders wrong.

```bash
cd frontend && npm test                       # vitest, pure logic under lib/
cd frontend && npm run build && npm run smoke # headless Chrome, computed styles
```

`make test` runs vitest alongside pytest; the smoke test needs a build first and belongs to CI.

---

## Where to find what

Three documents at the repo root; `docs/` holds assets, not documents
([§2](./ARCHITECTURE.md#2-repo-layout)). Adding a fourth means deleting or folding in another.

| Looking for | Where it lives |
| --- | --- |
| Layering, feature module shape, the reader/writer split | [§3 Backend architecture](./ARCHITECTURE.md#3-backend-architecture) |
| Why reads are unscoped and writes check ownership | [§4 The authorization contract](./ARCHITECTURE.md#4-the-authorization-contract--public-read-owner-write) |
| Migration policy, and why Prisma never runs in production | [§6 Database and migration policy](./ARCHITECTURE.md#6-database-and-migration-policy) |
| Deploy pipeline, what a failed job means, rolling back | [§7 Deploy policy](./ARCHITECTURE.md#7-deploy-policy) |
| Provisioned infrastructure — Supabase, Upstash, Fly, Vercel | [§1 System overview](./ARCHITECTURE.md#1-system-overview) |
| Which secret lives in which store, and how to rotate one | [§9 Secrets hygiene](./ARCHITECTURE.md#9-secrets-hygiene) |
| Enabling publish-to-GitHub, and registering the App | [§3.9 The publish feature](./ARCHITECTURE.md#39-the-publish-feature) |
| The `crawl-payloads` bucket, including its manual bootstrap | [§3.7 The storage infrastructure layer](./ARCHITECTURE.md#37-the-storage-infrastructure-layer) |
| Rules for working in this repo, and a per-feature checklist | [`CLAUDE.md`](./CLAUDE.md) |
