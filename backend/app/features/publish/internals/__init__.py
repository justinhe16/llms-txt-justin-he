"""Internals of the publish feature. Nothing outside `app.features.publish` imports from here
(ARCHITECTURE.md §3.1) — callers go through `PublishService`.

Four modules, split by what each is allowed to touch:

* `github_app.py` — the private key and the token exchange. The only module that reads a secret.
* `github_client.py` — the GitHub REST calls, every one of them taking a token as an argument so
  none of them can mint one.
* `change_summary.py` — pure, reads a run's `stats` to decide whether to publish and what the
  commit message says.
* `publish_reader.py` / `publish_writer.py` — every `SELECT`, and every write.
"""
