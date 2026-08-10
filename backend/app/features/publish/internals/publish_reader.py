"""Every `SELECT` for the publish feature. No writes, ever (ARCHITECTURE.md §3.1).

**Reads are unscoped, writes are ownership-checked — with one exception this module has to make,
and it is not a breach of §4.1.** `github_installations` is the only table in this codebase that
carries a `user_id` a read filters on, and the reason is that an installation is not a *site*.
§4.1's rule exists so every signed-in user can read every website and every run — public-ish
facts about crawled sites. An installation is a fact about a person's GitHub account, and
`get_installation_for_user` therefore takes a `user_id` and filters by it.

The distinction that keeps this honest: `publish_targets` and `publications` are read UNSCOPED,
exactly like `schedules` and `runs`, because they describe a website. Only the installation list
— "which GitHub accounts has *this* user connected?" — is per-user, and that read never appears
inside a website-scoped endpoint.

`_TARGET_COLUMNS` is spelled out rather than `SELECT *`, the same discipline
`schedules_reader.py` states at length: a column added to `publish_targets` for a later feature
cannot leak into a response through a reader nobody re-read.
"""

from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Reader


_TARGET_COLUMNS: Final = """
    t.id, t.website_id, t.installation_row_id, t.repo_owner, t.repo_name,
    t.base_branch, t.path, t.mode, t.active
"""

_INSTALLATION_COLUMNS: Final = "id, user_id, installation_id, account_login, created_at"

_LIST_INSTALLATIONS: Final = f"""
    SELECT {_INSTALLATION_COLUMNS}
    FROM github_installations
    WHERE user_id = $1
    ORDER BY created_at DESC
"""

_GET_INSTALLATION_FOR_USER: Final = f"""
    SELECT {_INSTALLATION_COLUMNS}
    FROM github_installations
    WHERE id = $1 AND user_id = $2
"""

# Joined to `github_installations` so one read answers both "where does this publish?" and "as
# which account?". The join is INNER rather than LEFT because the foreign key is `NOT NULL` with
# `ON DELETE RESTRICT` — a target without an installation is not a state the schema permits, and
# a LEFT JOIN here would be defending against a row that cannot exist while quietly making
# `account_login` nullable for every caller.
_GET_TARGET_BY_WEBSITE: Final = f"""
    SELECT {_TARGET_COLUMNS}, i.installation_id AS github_installation_id, i.account_login
    FROM publish_targets t
    JOIN github_installations i ON i.id = t.installation_row_id
    WHERE t.website_id = $1
"""

# The worker's entry point. It holds a run id and nothing else, so the join walks
# runs -> websites -> publish_targets rather than making `crawl_task` carry a `website_id` it has
# no other use for. `websites` is in the path because `publish_targets` keys on `website_id`.
_GET_TARGET_BY_RUN: Final = f"""
    SELECT {_TARGET_COLUMNS}, i.installation_id AS github_installation_id, i.account_login
    FROM runs r
    JOIN publish_targets t ON t.website_id = r.website_id
    JOIN github_installations i ON i.id = t.installation_row_id
    WHERE r.id = $1
"""

_LIST_PUBLICATIONS: Final = """
    SELECT p.id, p.run_id, p.status, p.commit_sha, p.pr_url, p.pr_number, p.error, p.created_at
    FROM publications p
    JOIN publish_targets t ON t.id = p.target_id
    WHERE t.website_id = $1
    ORDER BY p.created_at DESC
    LIMIT $2
"""

# The idempotency read. A worker retrying a run that already published must not open a second
# pull request for the same artifact, and this is what it asks first. Filtered to the outcomes
# that actually wrote something: a previous `failed` attempt should be retried, while a
# `succeeded` or `skipped_unchanged` one is already the correct final state for that run.
_PUBLICATION_COLUMNS: Final = "id, run_id, status, commit_sha, pr_url, pr_number, error, created_at"

# Selects the full response shape rather than only the fields the idempotency decision needs, so
# a caller that wants to RETURN the settled publication does not need a second read to get the
# rest of it. One query, one mapping to `PublicationResponse`, no way for two reads of the same
# row to disagree.
_FIND_SETTLED_PUBLICATION: Final = f"""
    SELECT {_PUBLICATION_COLUMNS}
    FROM publications
    WHERE run_id = $1 AND target_id = $2 AND status <> 'failed'
    LIMIT 1
"""


class PublishReader(Reader):
    """Reads for installations, publish targets, and publication history."""

    async def list_installations(self, user_id: UUID) -> list[dict[str, Any]]:
        """Every installation this user has connected, newest first."""
        return await self.fetch_all(_LIST_INSTALLATIONS, user_id)

    async def get_installation_for_user(
        self, installation_row_id: UUID, user_id: UUID
    ) -> dict[str, Any] | None:
        """One installation, but only if this user connected it.

        Scoped in the query rather than fetched-then-checked, and that is deliberate even though
        it looks like the `require_owner` pattern's opposite. `require_owner` exists for resources
        whose *absence* and whose *ownership* deserve different answers — a website someone else
        owns is a `403`, not a `404`, because its existence is not a secret (§4.1). An
        installation someone else connected is different: telling a caller "that installation
        exists but is not yours" leaks which GitHub accounts other users have connected. So both
        cases collapse to one `None` here, and the service turns it into a `404`.
        """
        return await self.fetch_one(_GET_INSTALLATION_FOR_USER, installation_row_id, user_id)

    async def get_target_by_website(self, website_id: UUID) -> dict[str, Any] | None:
        """A website's publish target, with its installation's GitHub id and account login."""
        return await self.fetch_one(_GET_TARGET_BY_WEBSITE, website_id)

    async def get_target_by_run(self, run_id: UUID) -> dict[str, Any] | None:
        """The publish target for the website a run belongs to, or `None` if there is none."""
        return await self.fetch_one(_GET_TARGET_BY_RUN, run_id)

    async def list_publications(self, website_id: UUID, limit: int) -> list[dict[str, Any]]:
        """This website's publication history, newest first."""
        return await self.fetch_all(_LIST_PUBLICATIONS, website_id, limit)

    async def find_settled_publication(
        self, run_id: UUID, target_id: UUID
    ) -> dict[str, Any] | None:
        """A non-failed publication for this run and target, if one exists.

        What makes republishing idempotent — see `_FIND_SETTLED_PUBLICATION` on why `failed` rows
        do not count.
        """
        return await self.fetch_one(_FIND_SETTLED_PUBLICATION, run_id, target_id)
