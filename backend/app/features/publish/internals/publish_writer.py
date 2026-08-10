"""Every write for the publish feature. **Never commits** (ARCHITECTURE.md §5).

Each method below executes its statement against the handle it was constructed with and returns.
The `transaction()` that commits it is opened in `PublishService`, which is what lets a service
put two of these writes in one unit of work — `upsert_target` replacing a target and
`record_publication` beside it — without either method knowing the other exists.

**`upsert_installation` is the one write in this codebase keyed on a value GitHub chose.** Its
`ON CONFLICT (user_id, installation_id)` is what makes re-running the setup callback idempotent:
GitHub sends a user back through it on every re-installation and on every permission change, and
each of those must update the row rather than fail on a unique violation or create a duplicate.
"""

from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Writer


_TARGET_COLUMNS: Final = """
    id, website_id, installation_row_id, repo_owner, repo_name, base_branch, path, mode, active
"""

_UPSERT_INSTALLATION: Final = """
    INSERT INTO github_installations (user_id, installation_id, account_login)
    VALUES ($1, $2, $3)
    ON CONFLICT (user_id, installation_id) DO UPDATE
        SET account_login = EXCLUDED.account_login
    RETURNING id, user_id, installation_id, account_login, created_at
"""

# `ON CONFLICT (website_id)` rather than a DELETE-then-INSERT, so the row's `id` survives a
# reconfiguration — `publications.target_id` references it, and a new id on every edit would
# orphan the history behind a foreign key that forbids exactly that (`ON DELETE RESTRICT`).
_UPSERT_TARGET: Final = f"""
    INSERT INTO publish_targets (
        website_id, installation_row_id, repo_owner, repo_name, base_branch, path, mode, active
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7::publish_mode, $8)
    ON CONFLICT (website_id) DO UPDATE
        SET installation_row_id = EXCLUDED.installation_row_id,
            repo_owner          = EXCLUDED.repo_owner,
            repo_name           = EXCLUDED.repo_name,
            base_branch         = EXCLUDED.base_branch,
            path                = EXCLUDED.path,
            mode                = EXCLUDED.mode,
            active              = EXCLUDED.active
    RETURNING {_TARGET_COLUMNS}
"""

_DELETE_TARGET: Final = """
    DELETE FROM publish_targets
    WHERE website_id = $1
"""

_DELETE_TARGETS_FOR_INSTALLATION: Final = """
    DELETE FROM publish_targets
    WHERE installation_row_id = $1
"""

_DELETE_INSTALLATION: Final = """
    DELETE FROM github_installations
    WHERE id = $1 AND user_id = $2
"""

_INSERT_PUBLICATION: Final = """
    INSERT INTO publications (
        run_id, target_id, status, commit_sha, pr_url, pr_number, error
    )
    VALUES ($1, $2, $3::publish_status, $4, $5, $6, $7)
    RETURNING id, run_id, status, commit_sha, pr_url, pr_number, error, created_at
"""


class PublishWriter(Writer):
    """Writes for installations, publish targets, and publications."""

    async def upsert_installation(
        self, user_id: UUID, installation_id: int, account_login: str
    ) -> dict[str, Any]:
        """Record (or refresh) an installation this user completed.

        Idempotent by `(user_id, installation_id)` — see the module docstring on why the setup
        callback needs that rather than an insert.
        """
        row = await self.fetch_one(_UPSERT_INSTALLATION, user_id, installation_id, account_login)
        # `RETURNING` on an upsert always produces a row: the `DO UPDATE` branch returns the
        # updated one and the insert branch the new one. `None` here would mean the statement
        # matched nothing at all, which this SQL has no path to — so it is a programming error,
        # not a state to hand back to a caller as `None`.
        assert row is not None, "upsert with RETURNING produced no row"
        return row

    async def upsert_target(
        self,
        website_id: UUID,
        *,
        installation_row_id: UUID,
        repo_owner: str,
        repo_name: str,
        base_branch: str,
        path: str,
        mode: str,
        active: bool,
    ) -> dict[str, Any]:
        """Create or replace this website's publish target, keeping the row's `id`."""
        row = await self.fetch_one(
            _UPSERT_TARGET,
            website_id,
            installation_row_id,
            repo_owner,
            repo_name,
            base_branch,
            path,
            mode,
            active,
        )
        assert row is not None, "upsert with RETURNING produced no row"
        return row

    async def delete_target(self, website_id: UUID) -> None:
        """Remove this website's publish target. A no-op when there is none."""
        await self.execute(_DELETE_TARGET, website_id)

    async def delete_targets_for_installation(self, installation_row_id: UUID) -> None:
        """Delete every publish target pointing at one installation.

        Exists so `PublishService.disconnect_installation` does not have to write SQL of its own
        (CLAUDE.md #6 — every write lives in the writer). It must run before
        `delete_installation` in the same transaction, because the foreign key is `ON DELETE
        RESTRICT` and the installation delete would otherwise raise.
        """
        await self.execute(_DELETE_TARGETS_FOR_INSTALLATION, installation_row_id)

    async def delete_installation(self, installation_row_id: UUID, user_id: UUID) -> None:
        """Disconnect an installation.

        Scoped by `user_id` in the statement as well as checked by the service, which is belt and
        braces rather than redundancy: this is a DELETE, and the cost of the two guards
        disagreeing is another user's row.

        Any `publish_targets` still pointing at this installation must be deleted first, in the
        same transaction — the foreign key is `ON DELETE RESTRICT`, so this statement raises
        rather than cascading. That is the intended design (see `schema.prisma`), and
        `PublishService.disconnect_installation` is where the ordering lives.
        """
        await self.execute(_DELETE_INSTALLATION, installation_row_id, user_id)

    async def record_publication(
        self,
        run_id: UUID,
        target_id: UUID,
        *,
        status: str,
        commit_sha: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Record how one publication attempt ended — including `skipped_unchanged`.

        A skipped attempt is written rather than passed over in silence, because the absence of a
        row cannot distinguish "we looked and nothing had changed" from "the schedule never ran"
        (`schema.prisma`'s own note on this table).
        """
        row = await self.fetch_one(
            _INSERT_PUBLICATION,
            run_id,
            target_id,
            status,
            commit_sha,
            pr_url,
            pr_number,
            error,
        )
        assert row is not None, "insert with RETURNING produced no row"
        return row
