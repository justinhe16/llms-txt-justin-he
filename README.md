# llms-text

Generate and maintain [`llms.txt`](https://llmstxt.org) files for websites. A signed-in user
registers a **website** by domain, the backend crawls it as a **run**, and each run produces an
`llms.txt` artifact describing the site in the form large language models can consume. Runs are
kept, so a website accumulates a history.

| | |
| --- | --- |
| **Live app** | **https://llms-text-justin-he-gamma.vercel.app** |
| API | https://llms-text-justin-he.fly.dev · [`/docs`](https://llms-text-justin-he.fly.dev/docs) · [`/health`](https://llms-text-justin-he.fly.dev/health) |
| Source | https://github.com/justinhe16/llms-txt-justin-he |

Sign in with GitHub, paste a URL, and the first run starts immediately. The Fly app and the
Vercel project keep their original `llms-text` hostnames — see "Infrastructure" below.

Next.js App Router on Vercel · FastAPI + an ARQ worker on Fly.io (one image, two processes) ·
Supabase for Postgres, Auth and Storage · Prisma for schema and migrations only, asyncpg at
runtime.

[`ARCHITECTURE.md`](./ARCHITECTURE.md) is the engineering contract — layout, layering, the
authorization model, transaction boundaries, migration and deploy policy, secrets hygiene. It is
the authority on all of it, and this file does not restate it.
[`CLAUDE.md`](./CLAUDE.md) is the short list of rules that are expensive to get wrong.

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
| Node | 20+ | `frontend/`, and the Prisma CLI in `db/` |
| Docker | current, and **running** | local Postgres, Auth, Storage, and Redis |
| Supabase CLI | **2.111.0, pinned** | the local Supabase stack |
| Fly CLI (`flyctl`) | current | only for inspecting deploys and setting secrets |

The Supabase CLI version is pinned, not just "current": `supabase/config.toml` was written and
verified against 2.111.0, and config keys and defaults have moved between CLI releases. Install
that exact version — `brew install supabase/tap/supabase` or `npm install -g supabase@2.111.0` —
and confirm with `supabase --version`.

### Setup

```bash
make setup            # once per checkout: backend/.venv, plus backend, db and frontend deps
supabase start        # Postgres, Auth, Storage — migrations need a live database
make migrate-apply    # apply db/migrations/ to the local database
make dev              # Supabase, Redis, the API, the worker, the frontend
```

Re-run `make setup` after pulling a dependency change. Everything after it is idempotent.

`make dev` starts Supabase itself, so on every run after the first it is the only command you
need. `supabase start` is listed separately above only because `make migrate-apply` reads its
connection string from a **running** stack and therefore has to come first. Supabase owns no
migrations here — `supabase/migrations/` is deliberately empty and Prisma owns the schema
([`ARCHITECTURE.md` §6](./ARCHITECTURE.md#6-database-and-migration-policy)) — so a database that
has never seen `make migrate-apply` has no application tables.

**There is no `.env` to fill in.** `make dev` derives `DATABASE_URL`, `SUPABASE_URL`,
`SUPABASE_SECRET_KEY` and `REDIS_URL` from the running stack (`scripts/local-env.sh`) and writes
`frontend/.env.local` for you, never putting a key in a tracked file. `backend/.env.example` and
`frontend/.env.example` document every variable, mark which are REQUIRED, and are the reference
for deployed environments; copy one to `backend/.env` only to override a default such as
`LOG_LEVEL=DEBUG`. If you run `npm --prefix frontend run dev` directly instead of through
`make dev`, run `scripts/local-env.sh write-frontend-env` first or the Supabase client throws on
first use.

### What you should see

`make dev` prints this once the stack is up:

```
llms-text dev environment is up:
  App              http://localhost:3000
  API              http://127.0.0.1:8000
  API docs         http://127.0.0.1:8000/docs
  Supabase Studio  http://localhost:54323
```

Then interleaved `[api]`, `[worker]` and `[frontend]` log lines. **All three matter.** `make dev`
starts the ARQ worker and **fails loudly if `arq` is missing** from the virtualenv rather than
skipping it, because a dev environment with no queue consumer looks exactly like one where
nothing is happening: enqueued jobs sit in Redis forever and the only symptom is silence. If it
stops that way, `make setup` refreshes the virtualenv.

`Ctrl-C` stops the API, the worker and the frontend. Supabase and Redis keep running in Docker,
so local data survives between sessions — `make down` stops those too.

### Local test user

Seeded by `supabase/seed.sql` when the local database first initializes, and again on every
`make reset`:

| Field | Value |
| --- | --- |
| Email | `dev@llms-text.test` |
| Password | `devpassword123` |

Sign in with the email/password form on `/`, which renders in development builds only. That
password unlocks a Postgres container on your own machine and nothing reachable from outside it.
GitHub OAuth is configured and verified in production but deliberately disabled locally
(`supabase/config.toml`), so email/password is the only local route in.

### Everyday commands

`make help` prints this list and is the source of truth for it:

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

`make test` runs the backend suite either way, but its database-backed tests only run when the
local Supabase stack is up; without it they skip with a message saying so, and the rest of the
suite is unaffected.

### Troubleshooting

**A port is already in use.** Supabase uses 54320–54324, Redis 6379, the API 8000, the frontend
3000. Find the holder with `lsof -nP -iTCP:<port> -sTCP:LISTEN`. For the API,
`API_PORT=8001 make dev` moves it for the session (`API_HOST` overrides the same way); the
Supabase ports are pinned in `supabase/config.toml` and should change there rather than on the
command line.

**Docker isn't running.** Every target that needs it checks for a live daemon — not just the
binary — before doing anything, and says so, rather than failing opaquely partway through.

**A stale container from a previous run.** `make down` is the clean shutdown. If something is
stuck anyway, find it with `docker ps -a`, `docker rm -f <name>`, then `make dev` again —
`supabase start` recreates whatever it needs.

**The local database is in an unknown state.** `make reset` recreates it, reseeds it, and
replays every migration.

---

## CI

Two workflows, one per stack, in [`.github/workflows/`](./.github/workflows). Where they overlap
with the `Makefile` they run the same commands `make lint` and `make test` run, so a green laptop
and a green pull request mean the same thing — change how a check is invoked in one and change it
in the other in the same pull request. CI is path-filtered per stack: `ci-backend.yml` does the
work for `backend/**` and `db/**`, `ci-frontend.yml` for `frontend/**`.

**The required status checks on `main` are the `backend-ci` and `frontend-ci` gate jobs, not the
jobs that do the work.** Both workflows start on every pull request and the path filter lives in
a `changes` job, so the expensive jobs are *skipped* rather than never started. That indirection
is load-bearing: a `paths:` trigger stops the workflow from starting at all, a workflow that
never starts never reports a check, and a required check that never reports blocks the merge
forever — which would leave every docs-only pull request permanently unmergeable. The filter is
[`.github/scripts/changed-paths.sh`](./.github/scripts/changed-paths.sh), and it has its own test
that both workflows run before trusting it.

`main` also requires linear history, so merge with `gh pr merge <n> --squash`, never a merge
commit.

The frontend is checked at three levels, because each one passes on failures the others catch.
`tsc` and eslint prove the types and the style line up. **Vitest** covers the pure logic under
`frontend/lib/` — comparators, status mappings, provenance arithmetic, URL validation — in a
node environment with no jsdom and no React, so it stays a unit-test suite rather than drifting
into a second end-to-end one. And a **rendered-output smoke test** loads the built page in
headless Chrome and measures what the browser actually resolved, because `tsc`, eslint and
`next build` all pass on a page that renders wrong:

```bash
cd frontend && npm test                       # vitest, pure logic under lib/
cd frontend && npm run build && npm run smoke # headless Chrome, computed styles
```

`make test` runs the vitest suite alongside the backend's pytest suite; the smoke test needs a
build first and so belongs to CI and to the command above.

---

## Deploy policy

**[`ARCHITECTURE.md` §7](./ARCHITECTURE.md#7-deploy-policy) is the authoritative copy — read the
policy there.** A deploy rule must never exist only in the README, so nothing here is new: this
is the shape of the pipeline, for an operator looking at a failure.

Merging to `main` with green CI is the only path to production. There is no manual promotion
step, and no `fly deploy` or `vercel --prod` from a laptop. A commit touching `backend/**` or
`db/**` runs [`deploy-backend.yml`](./.github/workflows/deploy-backend.yml) as three jobs, each
gated on the one before it:

| Job | What it does | What a failure means |
| --- | --- | --- |
| `migrate` | `prisma migrate deploy`, on a GitHub runner because the backend image is Python-only | **Nothing deployed.** Old code on the old schema — a consistent state, and the safest place to fail. Fix the migration and merge again. |
| `deploy` | `flyctl deploy --remote-only` from `backend/` | **The schema has already moved**: old code on the new schema. Survivable because migrations here are additive, but not a resting place. Fix forward. |
| `smoke` | `curl`s `/health` and reads the **body** — `db` must be `"ok"`, an unhealthy `redis` only warns | The new code is live but cannot reach Postgres. `fly logs --app llms-text-justin-he`. |

The `worker` process has no health check, because it has no HTTP listener — so a worker that dies
on startup is quiet and nothing goes red. After any change to its configuration, look:
`fly logs --app llms-text-justin-he --process worker`.

Vercel builds and deploys `frontend/` from its own git integration, outside this pipeline.

### Rolling back

**Code rolls back; migrations do not.** Revert the commit on `main` and let the pipeline deploy
the revert. Redeploying the previous image is break-glass — it buys availability while you
prepare that revert, and it is the only acknowledged exception to "never deploy by hand":

```bash
fly releases --app llms-text-justin-he                        # find the previous image
fly deploy --image <previous-image> --app llms-text-justin-he
fly scale count app=1 worker=1 --app llms-text-justin-he      # if a process group has no machines
```

It works only because [§6.3](./ARCHITECTURE.md#63-prohibitions) requires every migration to be
survivable by the release before it. Break that rule and the break-glass option is gone precisely
when you need it, because the old image would query columns that no longer exist.

---

## Infrastructure

One production environment, no staging. Everything below is provisioned.

| Service | Resource | Purpose |
| --- | --- | --- |
| Supabase | `iulfhmykutevtrgcaiec` · us-west-1 | Postgres, GitHub OAuth, private `crawl-payloads` bucket |
| Upstash | `llms-txt-prod` · us-west-1 | Redis backing the ARQ job queue |
| Fly.io | `llms-text-justin-he` · org `personal` | FastAPI API + ARQ worker, one image, two process groups |
| Vercel | `llms-text-justin-he` · scope `justinhe16s-projects` | Next.js frontend, root directory `frontend/` |

### Where each credential lives

No credential is stored in this repo, and none belongs in a pull request, an issue, a review
comment, or a log line ([`ARCHITECTURE.md` §9](./ARCHITECTURE.md#9-secrets-hygiene)). Each lives
in exactly one place:

| Name | Stored in | Where it comes from |
| --- | --- | --- |
| `DATABASE_URL` | Fly secrets | Supabase → Settings → Database → **Session pooler** |
| `REDIS_URL` | Fly secrets | `upstash redis get --db-id <id>` |
| `SUPABASE_URL` | Fly secrets, Vercel env | Supabase → Settings → API → Project URL |
| `SUPABASE_SECRET_KEY` | Fly secrets | Supabase → Settings → API → secret key |
| `ANTHROPIC_API_KEY` | Fly secrets | Anthropic Console → API keys. Required **only** when `CRAWL_ENRICH_WITH_LLM` is on; leave it unset otherwise |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Vercel env | Supabase → Settings → API → publishable key |
| `API_URL` | Vercel env | `https://llms-text-justin-he.fly.dev` — server-only, never `NEXT_PUBLIC_` |
| `FLY_API_TOKEN` | GitHub Actions secrets | `fly tokens create deploy -a llms-text-justin-he`, scoped to this app alone |
| `DIRECT_DATABASE_URL` | GitHub Actions secrets | The same string as `DATABASE_URL` |
| `GITHUB_APP_ID` | Fly secrets | The App's settings page. Not a secret, but set alongside the key |
| `GITHUB_APP_PRIVATE_KEY` | Fly secrets | The App's settings page → **Generate a private key**. Required only when `GITHUB_PUBLISH_ENABLED` is on |
| `GITHUB_APP_SLUG` | Fly secrets | The last path segment of the App's public URL |
| `NEXT_PUBLIC_GITHUB_APP_SLUG` | Vercel env | The same slug. Public by construction — it is in the App's own listing URL |

`CRAWL_ENRICH_WITH_LLM` defaults off, and neither it nor `ANTHROPIC_API_KEY` is set in CI or in
production today. With it off, nothing in the worker constructs an Anthropic client and every
run's `llms.txt` carries extraction's own titles and descriptions, unchanged from before PER-180
landed. Turning it on requires setting BOTH: the key alone does nothing (`CrawlService` never
reads it unless the flag is on), and the flag alone refuses to boot
(`Settings.validate_required_secrets` demands the key the moment the flag is `true`).

### Publishing to GitHub

Optional, off by default, and a deployment with none of it configured is a correct deployment:
`GITHUB_PUBLISH_ENABLED` gates the whole feature, and with it `false` nothing is read and nothing
is required at boot. CI runs that way deliberately.

Turning it on takes one manual step nothing in this repo can do for you — registering a GitHub App.

**1. Register the App.** GitHub → Settings → Developer settings → **GitHub Apps** → New GitHub App.

| Field | Value |
| --- | --- |
| Homepage URL | `https://llms-text-justin-he-gamma.vercel.app` |
| Callback URL | `https://llms-text-justin-he-gamma.vercel.app/api/github/callback` |
| Request user authorization (OAuth) during installation | on |
| Webhook | **off** — nothing in this system listens for one |

**Repository permissions — exactly two**, and no account permissions at all:

- **Contents: Read and write** — the commit
- **Pull requests: Read and write** — only needed for the default `pull_request` mode

Nothing else. The install prompt a user sees lists what you asked for, and every extra permission
is a reason for them to decline.

**2. Generate a private key** at the bottom of the App's settings page. It downloads a `.pem`.

**3. Set the secrets.** Never commit the key, never paste it into a PR or an issue, and never echo
it in a script ([CLAUDE.md rule 1](./CLAUDE.md)). It mints tokens that can **write to a user's
repository**, so a leak is a supply-chain problem — if it is ever exposed, revoking it on the App's
page and generating a new one is mandatory.

```bash
fly secrets set GITHUB_PUBLISH_ENABLED=true -a llms-text-justin-he
fly secrets set GITHUB_APP_ID=<the numeric id> -a llms-text-justin-he
fly secrets set GITHUB_APP_SLUG=<the-app-slug> -a llms-text-justin-he
fly secrets set GITHUB_APP_PRIVATE_KEY="$(cat ~/Downloads/<your-app>.private-key.pem)" -a llms-text-justin-he
```

Then set `NEXT_PUBLIC_GITHUB_APP_SLUG` to the same slug in Vercel, so the browser can build the
install link.

**What a user then does:** open a site's **Schedule** tab → **Connect a GitHub repository** →
choose which repositories to grant on GitHub → pick a repository, branch and path → turn on
**Publish on every successful run**. From then on, a run whose `llms.txt` differs from what the
repository already has opens a pull request. A run that finds no change writes nothing and records
`No change to publish`.

**Nothing is stored but a pointer.** `github_installations` holds an installation id and an account
name — no token, no key. Every repository write is authorized by a token minted from the App key at
the moment it is needed, held in memory, and left to expire. Uninstalling the App in GitHub's own
settings revokes access immediately, with nothing for this system to notice.

**Local development:** leave `GITHUB_PUBLISH_ENABLED=false`. Publishing needs a registered App and
a public callback URL, so it is not part of the local loop.

### Rotating

- **Supabase keys** — regenerate in the dashboard, then update Fly secrets and Vercel env
- **Redis** — `upstash redis reset-password --db-id <id>`, then re-set `REDIS_URL`
- **GitHub App private key** — generate a new one on the App's settings page, set `GITHUB_APP_PRIVATE_KEY`, then delete the old key on GitHub. Both are valid until you delete the old one, so there is no window where publishing breaks
- **Fly token** — `fly tokens revoke <id>`, re-create, `gh secret set FLY_API_TOKEN`
- **Anthropic key** — revoke in the console, create a replacement, `fly secrets set`

Pipe the value straight through so it never lands in a file or your shell history:

```bash
upstash redis get --db-id <id> \
  | jq -r '"rediss://default:\(.password)@\(.endpoint):\(.port)"' \
  | xargs -I{} fly secrets set REDIS_URL={} --app llms-text-justin-he
```

### The `crawl-payloads` bucket

A completed run's gzip-compressed JSONL payload is uploaded to a private Supabase Storage bucket
named `crawl-payloads`. Locally, `supabase/config.toml` declares it, so `make dev` provisions it
automatically and there is nothing to do on a fresh checkout.

**In production it is not provisioned by anything automated.** Nothing in CI and nothing in the
deploy pipeline creates it, so it is a manual bootstrap step, once, on a new Supabase project:
dashboard → Storage → New bucket → name it `crawl-payloads` → **Public: off**. Until that bucket
exists, every run fails its upload and ends `failed` with a sanitized error — so verify the
bucket exists before assuming a failed run is a code problem.

Known debt: deleting a website does not delete its Storage objects. `runs` rows cascade from
`websites`, but nothing sweeps the corresponding `{website_id}/` prefix in the bucket.
`ARCHITECTURE.md` §11 records the two candidate fixes; neither is built.

### Three things that will trip you up

1. **Use the Supabase session pooler on port 5432.** Not the direct connection (IPv6-only, so
   GitHub runners cannot reach it) and not port 6543 (transaction mode breaks asyncpg's prepared
   statements).
2. **The Vercel CLI defaults to the `dori` scope on this machine.** Every `vercel` command
   touching this project needs `--scope justinhe16s-projects`, or you will modify the wrong team.
3. **Fly secrets read `Staged` until the first deploy.** Expected — they were set with `--stage`
   and the app had no machines yet. CI's first deploy applies them.

---

## Documentation

Three documents at the repo root and no `docs/` directory
([`ARCHITECTURE.md` §2](./ARCHITECTURE.md#2-repo-layout)). Adding a fourth means deleting or
folding in another.

| Document | What it is |
| --- | --- |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | The engineering contract — layout, layering, authorization, transactions, migration and deploy policy, secrets hygiene, naming. It governs wherever this file and it disagree |
| [`CLAUDE.md`](./CLAUDE.md) | Rules for agents and humans working in this repo — the short list, plus a per-feature checklist |
| `README.md` | This file — what this is, how to run it, deploy policy |
