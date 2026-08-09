"""The publish feature: delivering a generated `llms.txt` into a GitHub repository.

**This feature owns three tables** — `github_installations`, `publish_targets` and
`publications` — which makes it the first feature module here to own any. `crawl` owns none and
writes through `runs`; this one has state of its own because "where does this site publish, and
what happened last time" outlives any single run.

**It holds no credential.** The GitHub App's private key is deployment configuration; an
installation access token is minted on demand and never written down. `internals/github_app.py`
is the only module that touches either, and its docstring is the argument for why the design is a
GitHub App rather than a stored OAuth or personal access token.

**A publish failure can never fail a crawl.** The call site is `app/worker/jobs.py`'s
`crawl_task`, AFTER `CrawlService.execute_run` has returned and the artifact is already committed
to `runs.llms_txt` — so there is no run in flight for a failure to affect. `PublishService.
publish_run` additionally never raises, recording a `failed` publication row instead.
"""
