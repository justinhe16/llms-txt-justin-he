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

[`ARCHITECTURE.md`](./ARCHITECTURE.md) is the engineering contract and wins wherever it and this
file disagree. [`CLAUDE.md`](./CLAUDE.md) is the short list of rules that are expensive to get
wrong.

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
| `npm audit` reports high/critical during `make setup` | Transitive **dev** dependencies, not shipped code. Not a blocker; setup still succeeded. |
| A port is already in use | Supabase 54320–54324, Redis 6379, API 8000, frontend 3000. Find it with `lsof -nP -iTCP:<port> -sTCP:LISTEN`. `API_PORT=8001 make dev` moves the API for the session (`API_HOST` likewise); change Supabase ports in `supabase/config.toml`, not on the command line. |
| Docker isn't running | Every target that needs it checks for a live daemon — not just the binary — and says so first. |
| A stale container | `make down` is the clean shutdown. If still stuck: `docker ps -a`, `docker rm -f <name>`, `make dev`. |
| The database is in an unknown state | `make reset` recreates, reseeds, and replays every migration. |
| A run sits at `pending` forever | Nothing is consuming the queue. No `ARQ worker ready` in `[worker]` means the worker died at startup; confirm Redis with `curl -s 127.0.0.1:8000/health`. |
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

## Deploy policy

**[§7](./ARCHITECTURE.md#7-deploy-policy) is authoritative.** Nothing here is new — this is the
shape of the pipeline, for an operator looking at a failure.

Merging to `main` with green CI is the only path to production. No manual promotion, no
`fly deploy` or `vercel --prod` from a laptop. A commit touching `backend/**` or `db/**` runs
[`deploy-backend.yml`](./.github/workflows/deploy-backend.yml) as three gated jobs:

| Job | What it does | What a failure means |
| --- | --- | --- |
| `migrate` | `prisma migrate deploy`, on a GitHub runner because the backend image is Python-only | **Nothing deployed.** Old code on the old schema — consistent, and the safest place to fail. Fix the migration and merge again. |
| `deploy` | `flyctl deploy --remote-only` from `backend/` | **The schema has already moved**: old code on the new schema. Survivable because migrations here are additive, but not a resting place. Fix forward. |
| `smoke` | `curl`s `/health` and reads the **body** — `db` must be `"ok"`, an unhealthy `redis` only warns | Live but cannot reach Postgres. `fly logs --app llms-text-justin-he`. |

The `worker` has no health check, because it has no HTTP listener — one that dies on startup is
quiet and nothing goes red. After any config change:
`fly logs --app llms-text-justin-he --process worker`.

Vercel builds and deploys `frontend/` from its own git integration, outside this pipeline.

### Rolling back

**Code rolls back; migrations do not.** Revert the commit on `main` and let the pipeline deploy
the revert. Redeploying the previous image is break-glass — it buys availability while you prepare
that revert, and is the only acknowledged exception to "never deploy by hand":

```bash
fly releases --app llms-text-justin-he                        # find the previous image
fly deploy --image <previous-image> --app llms-text-justin-he
fly scale count app=1 worker=1 --app llms-text-justin-he      # if a process group has no machines
```

It works only because [§6.3](./ARCHITECTURE.md#63-prohibitions) requires every migration to be
survivable by the release before it.

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

No credential is stored in this repo, and none belongs in a PR, an issue, a review comment, or a
log line ([§9](./ARCHITECTURE.md#9-secrets-hygiene)). Each lives in exactly one place.

| Name | Stored in | Where it comes from |
| --- | --- | --- |
| `DATABASE_URL` | Fly secrets | Supabase → Settings → Database → **Session pooler** |
| `REDIS_URL` | Fly secrets | `upstash redis get --db-id <id>` |
| `SUPABASE_URL` | Fly secrets, Vercel env | Supabase → Settings → API → Project URL |
| `SUPABASE_SECRET_KEY` | Fly secrets | Supabase → Settings → API → secret key |
| `ANTHROPIC_API_KEY` | Fly secrets | Anthropic Console → API keys. Required **only** when `CRAWL_ENRICH_WITH_LLM` is on |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Vercel env | Supabase → Settings → API → publishable key |
| `API_URL` | Vercel env | `https://llms-text-justin-he.fly.dev` — server-only, never `NEXT_PUBLIC_` |
| `FLY_API_TOKEN` | GitHub Actions secrets | `fly tokens create deploy -a llms-text-justin-he`, scoped to this app alone |
| `DIRECT_DATABASE_URL` | GitHub Actions secrets | The same string as `DATABASE_URL` |
| `GITHUB_APP_ID` | Fly secrets | The App's settings page. Not a secret, but set alongside the key |
| `GITHUB_APP_PRIVATE_KEY` | Fly secrets | The App's settings page → **Generate a private key**. Required only when `GITHUB_PUBLISH_ENABLED` is on |
| `GITHUB_APP_SLUG` | Fly secrets | The last path segment of the App's public URL |
| `NEXT_PUBLIC_GITHUB_APP_SLUG` | Vercel env | The same slug. Public by construction |

`CRAWL_ENRICH_WITH_LLM` defaults off and is not set in CI or production. Turning it on requires
BOTH it and the key: the key alone does nothing (`CrawlService` never reads it unless the flag is
on), and the flag alone refuses to boot (`Settings.validate_required_secrets`).

### Publishing to GitHub

Optional and off by default. `GITHUB_PUBLISH_ENABLED` gates the whole feature; with it `false`
nothing is read and nothing is required at boot, and CI runs that way deliberately. Turning it on
takes one manual step nothing in this repo can do for you — registering a GitHub App.

**1. Register the App.** GitHub → Settings → Developer settings → **GitHub Apps** → New GitHub App.

| Field | Value |
| --- | --- |
| Homepage URL | `https://llms-text-justin-he-gamma.vercel.app` |
| Setup URL | greyed out — see below |
| Callback URL | `https://llms-text-justin-he-gamma.vercel.app/api/github/callback` |
| Request user authorization (OAuth) during installation | on |
| Redirect on update | off — see below, it's a no-op in this configuration |
| Webhook | **off** — nothing here listens for one |

**Repository permissions — exactly two**, and no account permissions at all: **Contents: Read and
write** (the commit) and **Pull requests: Read and write** (the default `pull_request` mode).
Every extra permission is a reason for a user to decline the install.

**Setup URL vs. Callback URL, and why only one field is set.** GitHub distinguishes the two: the
setup URL is where an install lands (and, if **Redirect on update** is on, where a later change
to granted repositories lands too), the callback URL is where an *authorization* lands. Turning
on "Request user authorization (OAuth) during installation" folds the two into one flow — GitHub's
own [App-registration docs](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
say the toggle means "you will not be able to enter a URL here," and every post-install redirect
goes to the Callback URL instead, with a `code` appended. That is why this table sets only the
Callback URL: with the toggle on, the Setup URL field cannot be set at all — and the same docs say
**Redirect on update** "will be ignored" whenever Setup URL is blank, so checking it here would do
nothing. In this exact configuration, a user who revisits GitHub to change which repositories are
granted lands on GitHub's own installation-settings page, not back on the Publish tab, and there
is currently no way to change that while the OAuth toggle stays on.

If the toggle is ever turned off, set the Setup URL to the same `/api/github/callback` address —
the route reads only `installation_id`, `setup_action`, and `state`, none of which depend on how
the user arrived, so it works unchanged either way. That also makes **Redirect on update** live:
turn it on and GitHub will send the user back through that same Setup URL after they add or remove
a repository from an existing installation, not only after the initial install. They land on
`/crawls` rather than on a site's Publish tab, though, and that is not a gap to close here: `state`
is attached by the install link in `ConnectPrompt`, which only renders while the account has no
installation at all. A user editing an existing installation's repository access gets there through
GitHub's own settings page, which has no `state` to echo back — so `githubCallbackPath` sees none
and falls back to the list, exactly as it is designed to.

**The `code` this route never reads is a known gap, not an oversight.**
`PublishService.connect_installation` verifies the installation id against GitHub with this
deployment's own App credential before writing a row, which is what makes a hand-typed
`?installation_id=` a `400` rather than a silent forgery. That proves the installation *exists*;
it does not prove it belongs to the person the row is being recorded for. Exchanging the
`code` GitHub appends for a user access token and confirming the installation appears in that
user's own `GET /user/installations` is what would prove ownership — which is why the OAuth
toggle stays on even though nothing here reads the `code` yet. Flagged as a follow-up, not
implemented: exchanging that `code` needs its own ticket, not a line added to this one.

**2. Generate a private key** at the bottom of the App's settings page. It downloads a `.pem`.

**3. Set the secrets.** Never commit the key, paste it into a PR or issue, or echo it in a script
([CLAUDE.md rule 1](./CLAUDE.md)). It mints tokens that can **write to a user's repository**, so a
leak is a supply-chain problem — if exposed, revoking it and generating a new one is mandatory.

```bash
fly secrets set GITHUB_PUBLISH_ENABLED=true -a llms-text-justin-he
fly secrets set GITHUB_APP_ID=<the numeric id> -a llms-text-justin-he
fly secrets set GITHUB_APP_SLUG=<the-app-slug> -a llms-text-justin-he
fly secrets set GITHUB_APP_PRIVATE_KEY="$(cat ~/Downloads/<your-app>.private-key.pem)" -a llms-text-justin-he
```

Then set `NEXT_PUBLIC_GITHUB_APP_SLUG` to the same slug in Vercel, so the browser can build the
install link.

**What a user then does:** a site's **Publish** tab → **Connect a GitHub repository** → grant
repositories on GitHub → GitHub returns them to that site's **Publish** tab with a confirmation
→ pick a repository, branch and path → turn on **Publish on every successful run**. A run whose
`llms.txt` differs from what the repository has opens a pull request; a run that finds no change
writes nothing and records `No change to publish`.

**Nothing is stored but a pointer.** `github_installations` holds an installation id and an account
name — no token, no key. Every write is authorized by a token minted from the App key when needed,
held in memory, and left to expire. Uninstalling the App revokes access immediately.

**Local development:** leave `GITHUB_PUBLISH_ENABLED=false`. Publishing needs a registered App and
a public callback URL, so it is not part of the local loop.

### Rotating

- **Supabase keys** — regenerate in the dashboard, then update Fly secrets and Vercel env
- **Redis** — `upstash redis reset-password --db-id <id>`, then re-set `REDIS_URL`
- **GitHub App private key** — generate a new one, set `GITHUB_APP_PRIVATE_KEY`, then delete the old key on GitHub. Both are valid until you delete the old one, so publishing never breaks mid-rotation
- **Fly token** — `fly tokens revoke <id>`, re-create, `gh secret set FLY_API_TOKEN`
- **Anthropic key** — revoke in the console, create a replacement, `fly secrets set`

Pipe the value straight through so it never lands in a file or your shell history:

```bash
upstash redis get --db-id <id> \
  | jq -r '"rediss://default:\(.password)@\(.endpoint):\(.port)"' \
  | xargs -I{} fly secrets set REDIS_URL={} --app llms-text-justin-he
```

### The `crawl-payloads` bucket

A completed run's gzip-compressed JSONL payload goes to a private Supabase Storage bucket named
`crawl-payloads`. Locally `supabase/config.toml` declares it, so `make dev` provisions it.

**In production nothing provisions it.** It is a manual bootstrap step, once, on a new Supabase
project: dashboard → Storage → New bucket → `crawl-payloads` → **Public: off**. Until it exists
every run fails its upload and ends `failed` with a sanitized error — verify the bucket before
assuming a failed run is a code problem.

Known debt: deleting a website does not delete its Storage objects. `runs` rows cascade from
`websites`, but nothing sweeps the `{website_id}/` prefix. `ARCHITECTURE.md` §11 records two
candidate fixes; neither is built.

### Three things that will trip you up

1. **Use the Supabase session pooler on port 5432.** Not the direct connection (IPv6-only, so
   GitHub runners cannot reach it) and not port 6543 (transaction mode breaks asyncpg's prepared
   statements).
2. **The Vercel CLI defaults to the `dori` scope on this machine.** Every `vercel` command needs
   `--scope justinhe16s-projects`, or you will modify the wrong team.
3. **Fly secrets read `Staged` until the first deploy.** Expected — set with `--stage` when the
   app had no machines. CI's first deploy applies them.

---

## Documentation

Three documents at the repo root; `docs/` holds assets, not documents
([§2](./ARCHITECTURE.md#2-repo-layout)). Adding a fourth means deleting or folding in another.

| Document | What it is |
| --- | --- |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | The engineering contract — layout, layering, authorization, transactions, migration and deploy policy, secrets hygiene, naming. It governs wherever this file and it disagree |
| [`CLAUDE.md`](./CLAUDE.md) | Rules for agents and humans working in this repo — the short list, plus a per-feature checklist |
| `README.md` | This file — what this is, how to run it, deploy policy |
