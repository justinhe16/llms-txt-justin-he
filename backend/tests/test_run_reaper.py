"""Tests for `RunService.reap_stuck_runs` — the cron pass that rescues runs nothing is acting
on any more.

**Why this exists at all, restated because it is the thing the tests are protecting.** A run
stuck in `processing` does not merely look broken. `RunService.trigger_run`'s duplicate-run
guard refuses to start a run for a website that already has one in flight, so a single
stranded row makes that website permanently un-crawlable: every trigger answers `409` against
a run that will never finish. `test_a_website_blocked_by_a_stranded_run_becomes_triggerable_
again` is that acceptance criterion, executed.

**The global-state guard.** Like the cron tick, and unlike every API suite in this backend,
the reaper is NOT scoped to a test user — `RunsReader.lock_reapable` scans the whole `runs`
table on purpose. `websites_db` only cleans up rows owned by this suite's two fake users, so a
developer's local Supabase database can hold a real, long-abandoned `processing` run. That
would not merely skew a count here: the reaper would MUTATE it. `_assert_no_foreign_stuck_runs`
checks for exactly that and `pytest.skip`s rather than touching somebody's data.

**Every assertion is therefore written per-run rather than as a total** — "was THIS run
enqueued", "what does THIS row say now" — because a leftover `pending` row in a developer's
database is common, harmless (the reaper only reads those), and would otherwise turn every
`examined == 0` in this file into a coin flip.

**PER-198 — why `test_two_concurrent_reapers_enqueue_each_orphan_exactly_once` was flaky, and
what actually fixed it.** Before PER-198, that test `gather()`d two real passes, warmed the
pool, and trusted the event loop to interleave them — nothing forced the two `lock_reapable`
calls themselves to overlap. Confirmed, not assumed:

* **Measured overlap.** 50 isolated runs, instrumented at the `transaction()` boundary with
  `pg_backend_pid()` and `clock_timestamp()` (`clock_timestamp()` specifically — inside a
  transaction `now()` returns the transaction's start time, which would make every reading
  look like proof of whatever was expected). Two distinct backend pids and positive overlap
  (roughly 7-14ms) on every green iteration — and, in that same 50-run batch, one iteration
  measured a genuinely positive 6.8ms transaction-level overlap and STILL produced the flaky
  `assert 2 == 1`. That is the actual finding: transaction-lifetime overlap (`BEGIN` to
  `COMMIT`) does not guarantee the two `lock_reapable` SELECTs themselves overlap — Python's
  cooperative scheduling can let reaper 1 run its five guarded UPDATEs and commit inside the
  gap between reaper 2's `BEGIN` and its `SELECT`, even while both connections are, by a
  wall-clock measurement, "in a transaction" at the same instant.
* **Forced reproduction.** Serializing the two passes completely — reaper 2 not permitted to
  start until reaper 1's `transaction()` block had exited — with `FOR UPDATE SKIP LOCKED` left
  completely intact, reproduced the exact reported failure byte-for-byte on the first attempt:
  `AssertionError: two reapers both enqueued this orphan — FOR UPDATE SKIP LOCKED is not
  holding` / `assert 2 == 1`, with the lock working perfectly. The count was never wrong
  because of the lock; it was wrong because the two passes had not overlapped, and the second
  pass legitimately re-read orphan rows the first had already committed.

The fix, `_LockRendezvous` below, does not add a retry, a `sleep`, or a `flaky` marker — the
ticket ruled out all three, because each would hide a genuine `SKIP LOCKED` regression as
effectively as it hides the flake. It removes the race from the TEST HARNESS instead: reaper 2
cannot return from its own `lock_reapable` until reaper 1's has already returned (locks held,
transaction open, nothing committed), and reaper 1 is held inside that open transaction until
reaper 2's `lock_reapable` has also returned. The two locking attempts therefore provably
overlap on every run, not merely most of them — and the old assertion's message is no longer
allowed to fire without that overlap being proven first by the rendezvous assertions ahead of
it, which is what keeps a red run honest about what it actually means (see (5) in the mutation
list below).

**Whether the double-enqueue this test defends against is dangerous in production: no — it is
benign, and this is confirmed rather than assumed.** `RunService._enqueue_reaped` passes a
DETERMINISTIC job id (`crawl_job_id(run_id, attempts)`), and arq 0.28.0's own
`ArqRedis.enqueue_job` (`arq/connections.py`) enqueues inside a `WATCH job_key` /
`pipeline(transaction=True)` block and returns `None` — never raises — the instant either the
job key already exists or a `WatchError` fires because another caller enqueued it first under
the same key. That guarantee is Redis-atomic, not merely sequential: it holds for genuinely
concurrent callers, which is exactly what
`test_two_simultaneous_enqueues_of_the_same_job_id_produce_exactly_one_job` (below) drives
directly. See `RunService._enqueue_reaped`'s own docstring (`app/features/runs/service.py`)
for the same citation next to the code it documents.

**Mutation testing, recorded because the results are not what you would guess — and because
one of them corrects an earlier version of this docstring.** Both locking tests below were run
against `_LOCK_REAPABLE` with `SKIP LOCKED` deleted (mutation A) and with `FOR UPDATE SKIP
LOCKED` deleted outright (mutation B), each time reverted before the next:

1. `test_two_concurrent_reapers_enqueue_each_orphan_exactly_once` goes RED under BOTH — under
   A, at `_LockRendezvous`'s overlap-timeout assertion (reaper 2's `SELECT` blocks behind
   reaper 1's row locks and the 5s budget expires); under B, at the lock-disjointness assertion
   (reaper 2 locks rows reaper 1 is already holding, because nothing skips them any more). It
   is centred on the ORPHAN sweep deliberately: orphan rows are re-enqueued with no write, so a
   second reaper that reads them again has nothing to be stopped by. The `processing` rows in
   the same batch cannot detect either mutation, because `runs_writer.py`'s guarded UPDATEs
   already make a double-act a no-op — a real defence, just not this one.
2. `test_skip_locked_lets_the_reaper_past_a_row_someone_else_is_holding` ALSO goes RED under
   BOTH mutations, by TIMING OUT — **correcting this docstring's earlier claim that it failed
   only for the `SKIP LOCKED`-only mutation.** Measured directly for PER-198: deleting `FOR
   UPDATE SKIP LOCKED` outright leaves a bare `SELECT ... LIMIT $3` with no row lock at all, so
   `lock_reapable` itself no longer blocks on the held row — but the reader still returns it,
   and `RunsWriter.return_processing_to_pending` then issues its own guarded `UPDATE` against
   that row, which DOES take a write lock, and blocks on the holder exactly as it would under
   mutation A. The `asyncio.wait_for(..., timeout=5)` around the whole pass times out either
   way. It remains the one test that proves the production property `lock_reapable` actually
   needs — the reaper scans a table crawl workers are constantly locking rows in — even though
   it can no longer distinguish "the SELECT's own SKIP LOCKED is missing" from "the SELECT
   never took a row lock at all."

The queue is a `_RecordingQueuePool` in almost every test, mirroring `tests/
test_schedule_tick.py`'s stand-in: it records `(function, args, job_id)` without a network
round trip. The real `queue_pool` is used for exactly THREE tests, each because a stand-in
cannot answer the question it is asking: `test_the_second_pass_over_the_same_orphan_does_not_
queue_a_second_job` (arq's own refusal to enqueue a duplicate id, sequentially),
`test_two_passes_that_never_overlap_still_enqueue_each_orphan_once` (the same refusal, forced
fully serialized rather than merely sequential — PER-198's dedupe-not-locking proof), and
`test_two_simultaneous_enqueues_of_the_same_job_id_produce_exactly_one_job` (arq's own
`WatchError` branch, reached only under a genuine concurrent `gather()`).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from asyncpg import Connection, Pool
from conftest import (
    TEST_QUEUE_NAME,
    TEST_USER_A_ID,
    TEST_USER_B_ID,
    seed_run,
    seed_website,
)

from app.core.settings import settings
from app.features.runs.internals.runs_reader import RunsReader
from app.features.runs.service import (
    CRAWL_TASK_JOB_NAME,
    REAP_BATCH_LIMIT,
    RunService,
    build_run_service,
    crawl_job_id,
)
from app.infrastructure.db.transaction import transaction
from app.worker import jobs, policy
from app.worker.policy import MAX_ATTEMPTS
from app.worker.settings import WorkerSettings


_NOW = datetime.now(UTC)

# The two thresholds every test passes in, as absolute instants. Deliberately NOT the
# production values from `app/worker/policy.py`: a test that seeded a run "sixteen minutes
# ago" to clear a fifteen-minute threshold would silently stop testing anything the day the
# grace margin was raised. The job resolves policy into instants and hands them over
# (`reaper_tick`), so instants are the service's real contract and these are honest inputs.
_STUCK_BEFORE = _NOW - timedelta(minutes=10)
_ORPHANED_BEFORE = _NOW - timedelta(minutes=10)

# Comfortably older than both thresholds, and comfortably newer, for seeding either side.
_LONG_AGO = _NOW - timedelta(hours=1)
_JUST_NOW = _NOW - timedelta(seconds=5)


class _RecordingQueuePool:
    """A stand-in `ArqRedis` whose `enqueue_job` records instead of touching Redis.

    Returns a truthy sentinel, matching what arq returns when a job is genuinely enqueued —
    the reaper distinguishes that from `None` ("a job with this id already exists"), so a
    stand-in returning `None` would make every enqueue look like a deduplicated one.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, tuple[Any, ...], str | None]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> object:
        self.enqueued.append((function, args, kwargs.get("_job_id")))
        return object()

    def run_ids(self) -> list[str]:
        return [str(args[0]) for _function, args, _job_id in self.enqueued]


class _FailingQueuePool:
    """Raises on every `enqueue_job`, for the "a bad enqueue must not fail the run" test."""

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> object:
        raise ConnectionError("simulated Redis outage")


# How long `_LockRendezvous` (below) will wait for the loser to catch up before giving up and
# letting the winner proceed. Paid ONLY when the handoff fails — every green run resolves in
# a handful of milliseconds, the time for reaper 2 to acquire an already-warm connection, BEGIN,
# and run one SELECT. 5s is roughly three orders of magnitude of headroom over that, which is
# what makes a timeout here a strong signal rather than a "maybe the CI box was busy" shrug.
_OVERLAP_TIMEOUT_SECONDS = 5.0


class _LockRendezvous:
    """Makes the two reapers' `lock_reapable` calls DETERMINISTICALLY overlap, instead of
    merely hoping they do.

    **Why hoping is not enough — this is PER-198's whole finding.** The obvious version of
    `test_two_concurrent_reapers_enqueue_each_orphan_exactly_once` just `gather()`s two real
    passes and trusts the event loop to interleave them. Usually it does. Under load — a busy
    CI box, or this suite running back-to-back with everything else in it — the two coroutines
    can instead run one after the other: reaper 1's `SELECT ... FOR UPDATE SKIP LOCKED` locks
    every row, does its writes, and COMMITS, all before reaper 2's `lock_reapable` ever runs.
    Reaper 2 then legitimately re-reads the same now-committed, still-old `pending` rows and
    enqueues them again — `SKIP LOCKED` never even had a locked row to skip, because there was
    no overlap for it to defend. `enqueued.count(...) == 2` fires, and the assertion's own
    message blames `FOR UPDATE SKIP LOCKED` for a failure the lock had nothing to do with.
    Confirmed, not assumed, before this fix was written — see the module docstring's "root
    cause" section for the measured overlap and the forced-serialization reproduction.

    **The mechanism.** Patches the `RunsReader.lock_reapable` CLASS attribute (same shape as
    the `RunsWriter.mark_processing_failed` patch in
    `test_the_reaper_rescues_a_run_whose_own_failure_write_could_not_land` above), wrapping the
    ORIGINAL method captured before the patch — `reap_stuck_runs` constructs a fresh
    `RunsReader(tx)` per pass, so patching the class is what reaches both instances. The
    guarded-`Pool` check inside the real `lock_reapable` still runs, because the wrapper calls
    straight through to it.

    A call counter, incremented before the `await`, tells call #1 (the winner, locks taken,
    transaction open, nothing committed yet) apart from call #2 (the loser, whose own
    `lock_reapable` cannot even return until call #1 has set `first_locked`). Call #1 then
    BLOCKS on `second_locked` before it returns — so reaper 1's transaction is GUARANTEED still
    open (it has not even left the `async with transaction(...)` block) at the exact moment
    reaper 2's locked `SELECT` completes. That is a stronger guarantee than "the two coroutines
    were scheduled close together": it is "reaper 2's lock attempt provably ran while reaper
    1's locks were provably still held."

    **The timeout must NOT raise.** Under a real `SKIP LOCKED` regression, reaper 2's `SELECT`
    blocks on reaper 1's row locks while reaper 1 is blocked waiting on `second_locked` — a
    genuine cycle, since neither call can proceed. Call #1 records `timed_out = True` and
    returns normally instead of raising, which lets reaper 1 commit (releasing the locks reaper
    2's blocked `SELECT` is waiting on) rather than leaving both coroutines deadlocked until
    pytest's own runner kills the process.
    """

    def __init__(self) -> None:
        self.first_locked = asyncio.Event()
        self.second_locked = asyncio.Event()
        self.calls = 0
        # Which run ids each call locked, keyed by call number (1 or 2) — what lets the test
        # assert the two calls' locked rows are disjoint, independent of the enqueue-count
        # assertion below it.
        self.locked_ids: dict[int, set[str]] = {}
        self.timed_out = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = RunsReader.lock_reapable

        async def _gated(
            reader: RunsReader,
            *,
            stuck_before: datetime,
            orphaned_before: datetime,
            limit: int,
        ) -> list[dict[str, Any]]:
            self.calls += 1
            which = self.calls
            rows = await original(
                reader, stuck_before=stuck_before, orphaned_before=orphaned_before, limit=limit
            )
            self.locked_ids[which] = {str(row["id"]) for row in rows}
            if which == 1:
                self.first_locked.set()
                try:
                    await asyncio.wait_for(
                        self.second_locked.wait(), timeout=_OVERLAP_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    self.timed_out = True
            else:
                self.second_locked.set()
            return rows

        monkeypatch.setattr(RunsReader, "lock_reapable", _gated)


class _CommitGate:
    """Forces the SERIALIZED case on purpose — the exact condition that made the un-fixed test
    flaky (see `_LockRendezvous` above) — by blocking a second caller until a first caller's
    `transaction()` block has committed.

    Patches `app.features.runs.service.transaction` itself (string-target `monkeypatch.setattr`,
    the same indirection `test_the_reaper_rescues_a_run_whose_own_failure_write_could_not_land`
    above uses for `RunsWriter.mark_processing_failed`), delegating to the real `transaction`
    so every statement inside still runs for real — only the moment its caller learns the block
    has exited is instrumented. Used by
    `test_two_passes_that_never_overlap_still_enqueue_each_orphan_once`, which needs the
    opposite of `_LockRendezvous`: proof that two passes with NO overlap at all still produce
    exactly one job per orphan, because arq's own dedupe — not `FOR UPDATE SKIP LOCKED` — is
    what makes that true.
    """

    def __init__(self) -> None:
        self.first_committed = asyncio.Event()
        self._calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        @asynccontextmanager
        async def _gated(pool: Pool) -> AsyncIterator[Connection]:
            self._calls += 1
            which = self._calls
            async with transaction(pool) as conn:
                yield conn
            if which == 1:
                self.first_committed.set()

        monkeypatch.setattr("app.features.runs.service.transaction", _gated)


@pytest.fixture
def run_service(websites_db: Pool) -> RunService:
    """The exact object `app.worker.jobs.reaper_tick` builds in production, against this
    suite's database."""
    return build_run_service(websites_db, settings)


async def _assert_no_foreign_stuck_runs(pool: Pool) -> None:
    """Skip loudly if a `processing` run outside this suite's two test users would be REAPED.

    See the module docstring's "global-state guard" section. Scoped to `processing`
    deliberately, and the asymmetry is the point:

    * A foreign `processing` row would be WRITTEN to — flipped back to `pending`, or marked
      `failed`. That is somebody's data being changed by a test run, and this suite refuses
      rather than do it.
    * A foreign orphaned `pending` row is only READ and re-enqueued, onto a
      `_RecordingQueuePool` that goes nowhere. Nothing about the developer's database
      changes, so skipping over it would cost the whole suite for no protection. Every
      assertion below is written per-run — "was THIS run enqueued", not "how many runs were
      enqueued" — precisely so an unrelated `pending` row in a local database cannot make
      this file red or, worse, quietly green for the wrong reason.
    """
    count = await pool.fetchval(
        """
        SELECT count(*)
        FROM runs r
        JOIN websites w ON w.id = r.website_id
        WHERE r.status IN ('pending', 'processing')
          AND r.status = 'processing'
          AND COALESCE(r.claimed_at, r.started_at) < $2
          AND w.user_id != ALL($1::uuid[])
        """,
        [TEST_USER_A_ID, TEST_USER_B_ID],
        _STUCK_BEFORE,
    )
    if count:
        pytest.skip(
            f"{count} `processing` run(s) outside this suite's two test users would be "
            "reaped in TEST_DATABASE_URL. The reaper is not scoped to a user (by design — "
            "see RunsReader.lock_reapable) and it WRITES to what it finds, so this suite "
            "refuses to run rather than modify them. Clean up TEST_DATABASE_URL's `runs` "
            "table (or point TEST_DATABASE_URL at a fresh database) and re-run."
        )


async def _reap(service: RunService, queue: object, *, max_attempts: int = MAX_ATTEMPTS) -> Any:
    return await service.reap_stuck_runs(
        queue,  # type: ignore[arg-type]
        stuck_before=_STUCK_BEFORE,
        orphaned_before=_ORPHANED_BEFORE,
        max_attempts=max_attempts,
    )


async def _row(pool: Pool, run_id: UUID) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT status, error, attempts, claimed_at, completed_at FROM runs WHERE id = $1",
        run_id,
    )
    assert row is not None
    return dict(row)


async def _seed_stuck(
    pool: Pool,
    suffix: str,
    *,
    attempts: int = 1,
    claimed_at: datetime | None = _LONG_AGO,
    started_at: datetime = _LONG_AGO,
) -> tuple[UUID, UUID]:
    """A `processing` run whose worker never came back."""
    website_id = await seed_website(pool, TEST_USER_A_ID, f"https://reaper-{suffix}.example")
    run_id = await seed_run(
        pool,
        website_id,
        started_at=started_at,
        status="processing",
        attempts=attempts,
        claimed_at=claimed_at,
    )
    return website_id, run_id


async def _seed_orphan(
    pool: Pool, suffix: str, *, started_at: datetime = _LONG_AGO, attempts: int = 0
) -> tuple[UUID, UUID]:
    """A `pending` run with (as far as anything can tell) no job behind it."""
    website_id = await seed_website(pool, TEST_USER_A_ID, f"https://reaper-{suffix}.example")
    run_id = await seed_run(
        pool, website_id, started_at=started_at, status="pending", attempts=attempts
    )
    return website_id, run_id


# -----------------------------------------------------------------------------------------
# 1. A stuck `processing` run, with budget left.
# -----------------------------------------------------------------------------------------


async def test_a_stuck_run_under_the_attempt_cap_returns_to_pending_and_is_re_enqueued(
    websites_db: Pool, run_service: RunService
) -> None:
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(websites_db, "requeue", attempts=1)
    queue = _RecordingQueuePool()

    summary = await _reap(run_service, queue)

    row = await _row(websites_db, run_id)
    assert row["status"] == "pending"
    assert row["attempts"] == 1, "the abandoned attempt still counts against the budget"
    assert row["claimed_at"] is None, "the next claim gets a full threshold of its own"
    assert row["error"] is None, "a rescue is not a failure"
    assert row["completed_at"] is None

    assert queue.run_ids().count(str(run_id)) == 1
    assert summary.requeued == 1
    assert summary.failed == 0


async def test_a_requeued_run_is_enqueued_under_the_deterministic_job_id(
    websites_db: Pool, run_service: RunService
) -> None:
    """The id is `crawl:{run_id}:{attempts}`, and `attempts` is part of it deliberately — see
    `RunService._enqueue_reaped`. Asserted through `crawl_job_id` rather than by rebuilding
    the format string here, so the two cannot drift."""
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(websites_db, "job-id", attempts=2)
    queue = _RecordingQueuePool()

    await _reap(run_service, queue)

    (function, args, job_id) = next(entry for entry in queue.enqueued if entry[1] == (str(run_id),))
    assert function == CRAWL_TASK_JOB_NAME
    assert job_id == crawl_job_id(run_id, 2)


# -----------------------------------------------------------------------------------------
# 2. A stuck `processing` run at the cap.
# -----------------------------------------------------------------------------------------


async def test_a_stuck_run_at_the_attempt_cap_is_marked_failed(
    websites_db: Pool, run_service: RunService
) -> None:
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(websites_db, "give-up", attempts=MAX_ATTEMPTS)
    queue = _RecordingQueuePool()

    summary = await _reap(run_service, queue)

    row = await _row(websites_db, run_id)
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    error = row["error"]
    assert isinstance(error, str)
    assert str(MAX_ATTEMPTS) in error, "the message says how many attempts were made"

    assert str(run_id) not in queue.run_ids(), "a run that is done must not be re-queued"
    assert summary.failed == 1
    assert summary.requeued == 0


# -----------------------------------------------------------------------------------------
# 3. Runs the reaper must LEAVE ALONE. The expensive mistake is in this direction.
# -----------------------------------------------------------------------------------------


async def test_a_run_claimed_within_the_threshold_is_left_alone(
    websites_db: Pool, run_service: RunService
) -> None:
    """Reaping a run that is legitimately still working duplicates a crawl; reaping one late
    costs five minutes. The costs are not symmetric, so this is the assertion the threshold
    exists for."""
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(websites_db, "still-working", claimed_at=_JUST_NOW)
    before = await _row(websites_db, run_id)
    queue = _RecordingQueuePool()

    summary = await _reap(run_service, queue)

    assert await _row(websites_db, run_id) == before
    assert str(run_id) not in queue.run_ids()
    assert summary.requeued == 0
    assert summary.failed == 0


async def test_a_long_queued_run_claimed_moments_ago_is_left_alone(
    websites_db: Pool, run_service: RunService
) -> None:
    """THE `claimed_at`-VS-`started_at` TEST, and the sharpest "too aggressive" case there is.

    `started_at` is set at INSERT, which is when the run was ENQUEUED. With `MAX_JOBS = 2` and
    a cron tick that can create up to `DUE_BATCH_LIMIT` runs at once, a run can legitimately
    sit `pending` for an hour before any worker touches it. A reaper measuring staleness from
    `started_at` — which is what the ticket's own example SQL does — would then reap it
    seconds after a worker finally picked it up: two workers crawling the same site, two
    payloads uploaded, one orphaned, and a `mark_processing_completed` that silently updates
    zero rows.

    So: an ancient `started_at` with a fresh `claimed_at`, and the reaper must not touch it.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(
        websites_db, "backlogged", started_at=_LONG_AGO, claimed_at=_JUST_NOW
    )
    queue = _RecordingQueuePool()

    summary = await _reap(run_service, queue)

    assert (await _row(websites_db, run_id))["status"] == "processing"
    assert str(run_id) not in queue.run_ids()
    assert summary.requeued == 0
    assert summary.failed == 0


async def test_a_recently_created_pending_run_is_not_swept_as_an_orphan(
    websites_db: Pool, run_service: RunService
) -> None:
    """A `pending` run whose job is simply still in the queue, or which is waiting out its own
    retry backoff, is not an orphan. Sweeping it early would enqueue a second job for work
    that was never lost."""
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_orphan(websites_db, "fresh-pending", started_at=_JUST_NOW)
    queue = _RecordingQueuePool()

    await _reap(run_service, queue)

    assert (await _row(websites_db, run_id))["status"] == "pending"
    assert str(run_id) not in queue.run_ids(), "its job is simply still in the queue"


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_a_terminal_run_is_never_examined_however_old(
    websites_db: Pool, run_service: RunService, status: str
) -> None:
    """The partial index the scan rides on covers `pending` and `processing` only, and so does
    the reaper's contract. A years-old `completed` row is not stranded, it is history."""
    await _assert_no_foreign_stuck_runs(websites_db)
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://reaper-terminal-{status}.example"
    )
    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_LONG_AGO,
        status=status,
        completed_at=_LONG_AGO,
        attempts=1,
        claimed_at=_LONG_AGO,
    )
    before = await _row(websites_db, run_id)

    queue = _RecordingQueuePool()
    await _reap(run_service, queue)

    assert await _row(websites_db, run_id) == before
    assert str(run_id) not in queue.run_ids()


async def test_a_processing_row_from_before_the_migration_is_still_reachable(
    websites_db: Pool, run_service: RunService
) -> None:
    """`COALESCE(claimed_at, started_at)`, and why a bare `claimed_at < $1` would be a bug.

    Every row that reached `processing` before PER-166's migration has `claimed_at` NULL, and
    `NULL < $1` is not true in SQL — so a bare comparison would make exactly the runs stranded
    by the old code permanently invisible to the reaper built to rescue them.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(
        websites_db, "pre-migration", attempts=0, claimed_at=None, started_at=_LONG_AGO
    )

    await _reap(run_service, _RecordingQueuePool())

    assert (await _row(websites_db, run_id))["status"] == "pending"


# -----------------------------------------------------------------------------------------
# 4. Orphaned `pending` runs — the insert-then-enqueue window.
# -----------------------------------------------------------------------------------------


async def test_an_orphaned_pending_run_is_re_enqueued_without_being_written_to(
    websites_db: Pool, run_service: RunService
) -> None:
    """It is already in the state it should be in. What it is missing is a job, and no UPDATE
    can hand it one — so the reaper does not write to it at all."""
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_orphan(websites_db, "orphan")
    before = await _row(websites_db, run_id)
    queue = _RecordingQueuePool()

    summary = await _reap(run_service, queue)

    assert await _row(websites_db, run_id) == before
    assert queue.run_ids().count(str(run_id)) == 1
    assert summary.orphans_swept >= 1
    assert summary.requeued == 0


async def test_an_enqueue_failure_leaves_the_run_pending_rather_than_failing_it(
    websites_db: Pool, run_service: RunService
) -> None:
    """Unlike the cron tick's enqueue path, a Redis blip here must NOT mark the run failed.
    The run is legitimately `pending` and has done nothing wrong; the next pass tries again in
    five minutes, which is a strictly better answer than a terminal row."""
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_orphan(websites_db, "enqueue-fails")

    summary = await _reap(run_service, _FailingQueuePool())

    assert (await _row(websites_db, run_id))["status"] == "pending"
    assert summary.enqueue_failures >= 1
    assert summary.enqueued == 0


async def test_the_second_pass_over_the_same_orphan_does_not_queue_a_second_job(
    websites_db: Pool, run_service: RunService, queue_pool: ArqRedis
) -> None:
    """THE ANTI-AMPLIFICATION TEST, and the reason the reaper passes `_job_id` at all.

    An orphan sweep writes nothing, so nothing stops the next pass — five minutes later —
    from finding the same row and enqueuing another job, and the pass after that a third. A
    genuinely backlogged queue would collect one junk job per run per five minutes for as long
    as the backlog lasted.

    Driven against the REAL Redis, because arq's own refusal to enqueue a job whose id already
    exists is the mechanism under test; a stand-in would only be asserting that this test
    knows what it hopes arq does.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    await queue_pool.flushdb()
    try:
        _website_id, run_id = await _seed_orphan(websites_db, "no-amplification")

        first = await _reap(run_service, queue_pool)
        after_first = await queue_pool.zcard(TEST_QUEUE_NAME)
        second = await _reap(run_service, queue_pool)
        after_second = await queue_pool.zcard(TEST_QUEUE_NAME)

        assert first.enqueued >= 1
        assert second.enqueued == 0, "the second pass must not add a second job"
        assert second.already_queued >= 1
        assert second.orphans_swept >= 1, "it still examined the row; it just did not requeue"

        # The queue itself, not the summary: the second pass added nothing at all, and the
        # job the first pass created is still there under its deterministic id.
        assert after_second == after_first
        assert await queue_pool.zscore(TEST_QUEUE_NAME, crawl_job_id(run_id, 0)) is not None
    finally:
        await queue_pool.flushdb()


async def test_two_passes_that_never_overlap_still_enqueue_each_orphan_once(
    websites_db: Pool,
    run_service: RunService,
    queue_pool: ArqRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forces the SERIALIZED case — no overlap between the two passes at all, the exact
    condition that made `test_two_concurrent_reapers_enqueue_each_orphan_exactly_once` flaky
    before PER-198 (see this module's docstring) — and proves that against REAL Redis the
    outcome is still exactly one job per orphan.

    **Deliberately lock-independent — this is not redundant `SKIP LOCKED` coverage.** With
    zero overlap there is nothing for `FOR UPDATE SKIP LOCKED` to defend: reaper 1 commits
    fully, via `_CommitGate`, before reaper 2's `lock_reapable` even runs, so reaper 2
    legitimately re-reads the same, still-old `pending` rows — the identical situation the
    flaky version of the headline test stumbled into by accident. What stops reaper 2 from
    enqueuing a second job for each row is arq's own refusal to enqueue a job under an id that
    already exists (`RunService._enqueue_reaped`'s `_job_id=crawl_job_id(run_id, attempts)`),
    not the database lock — this test passes identically whether `_LOCK_REAPABLE` has `FOR
    UPDATE SKIP LOCKED`, `FOR UPDATE`, or nothing at all. It pins "dedupe, not `SKIP LOCKED`,
    is what makes a concurrent — or merely un-overlapped — orphan sweep safe against Redis,"
    which `test_two_concurrent_reapers_enqueue_each_orphan_exactly_once` does not test.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    await queue_pool.flushdb()
    try:
        orphan_ids = [
            (await _seed_orphan(websites_db, f"serialized-orphan-{i}"))[1] for i in range(5)
        ]

        gate = _CommitGate()
        gate.install(monkeypatch)

        async def _second_after_commit() -> Any:
            await gate.first_committed.wait()
            return await _reap(run_service, queue_pool)

        first, second = await asyncio.gather(_reap(run_service, queue_pool), _second_after_commit())

        assert first.enqueued == 5
        assert second.enqueued == 0
        assert second.already_queued == 5
        assert second.orphans_swept >= 5, "it DID re-read them; arq is what stopped it"
        assert await queue_pool.zcard(TEST_QUEUE_NAME) == 5
        for run_id in orphan_ids:
            assert await queue_pool.zscore(TEST_QUEUE_NAME, crawl_job_id(run_id, 0)) is not None
    finally:
        await queue_pool.flushdb()


async def test_two_simultaneous_enqueues_of_the_same_job_id_produce_exactly_one_job(
    queue_pool: ArqRedis,
) -> None:
    """Reaches arq's own `WatchError` dedupe branch directly — the test above forces its two
    passes fully SERIAL and cannot get here on purpose. `gather()` of two `enqueue_job` calls
    under the SAME deterministic id, built from the reaper's own `CRAWL_TASK_JOB_NAME` /
    `crawl_job_id` so this cannot drift from what `_enqueue_reaped` actually passes.
    Deterministic under either interleaving: arq's `pipeline(transaction=True)` /
    `WATCH`/`MULTI`/`EXEC` (see `RunService._enqueue_reaped`'s docstring) makes exactly one of
    the two calls win, no matter which one Redis happens to service first. No `runs` row
    involved — this is Redis-only, so it needs neither `websites_db` nor the foreign-row guard.
    """
    await queue_pool.flushdb()
    try:
        run_id = uuid4()
        job_id = crawl_job_id(run_id, 0)

        results = await asyncio.gather(
            queue_pool.enqueue_job(CRAWL_TASK_JOB_NAME, str(run_id), _job_id=job_id),
            queue_pool.enqueue_job(CRAWL_TASK_JOB_NAME, str(run_id), _job_id=job_id),
        )

        assert sum(result is not None for result in results) == 1, (
            "exactly one of the two simultaneous enqueues should win under the same job id"
        )
        assert await queue_pool.zcard(TEST_QUEUE_NAME) == 1
    finally:
        await queue_pool.flushdb()


# -----------------------------------------------------------------------------------------
# 5. PER-163's gap — the case nothing else in the system can recover from.
# -----------------------------------------------------------------------------------------


async def test_the_reaper_rescues_a_run_whose_own_failure_write_could_not_land(
    websites_db: Pool, run_service: RunService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RunService.record_failure` opens a transaction and writes a terminal row. If Postgres
    is unreachable at that exact moment the write raises, the exception propagates out of the
    job, and the run stays `processing` forever — the process that knew what happened has no
    way left to record it, and no amount of in-process error handling can fix that.

    PER-163's reviewer identified this and concluded it was unrecoverable without exactly this
    reaper. So this test reproduces it end to end rather than asserting it in a comment: make
    the terminal write fail, confirm the run really is stranded, then run the reaper and
    confirm it is not.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    _website_id, run_id = await _seed_stuck(websites_db, "record-failure-gap", attempts=1)

    async def _raise(
        self: object, run_id: UUID, error: str, stats: dict[str, Any] | None = None
    ) -> str:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(
        "app.features.runs.internals.runs_writer.RunsWriter.mark_processing_failed", _raise
    )
    with pytest.raises(RuntimeError):
        await run_service.record_failure(run_id, "the crawl failed")

    # Stranded: the run is `processing`, nothing is working on it, and the duplicate-run guard
    # will now refuse every new trigger for this website.
    assert (await _row(websites_db, run_id))["status"] == "processing"

    monkeypatch.undo()
    queue = _RecordingQueuePool()
    await _reap(run_service, queue)

    row = await _row(websites_db, run_id)
    assert row["status"] == "pending", "the reaper is the only thing that could rescue this"
    assert queue.run_ids().count(str(run_id)) == 1


async def test_a_website_blocked_by_a_stranded_run_becomes_triggerable_again(
    websites_db: Pool, run_service: RunService
) -> None:
    """The acceptance criterion the whole ticket is for.

    `RunsReader.get_active_for_website` is what `trigger_run` consults before answering `409`.
    While the run is stranded it reports one in flight; once the reaper has given up on it,
    it reports nothing and the website can be crawled again. Asserted through the real reader
    rather than by re-querying `runs` here, so this tests the guard that actually gates the
    endpoint.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    website_id, _run_id = await _seed_stuck(websites_db, "unblocks", attempts=MAX_ATTEMPTS)

    reader = RunsReader(websites_db)
    assert await reader.get_active_for_website(website_id) is not None

    await _reap(run_service, _RecordingQueuePool())

    assert await reader.get_active_for_website(website_id) is None


# -----------------------------------------------------------------------------------------
# 6. Batching.
# -----------------------------------------------------------------------------------------


async def test_the_batch_limit_bounds_one_pass_and_the_remainder_waits(
    websites_db: Pool, run_service: RunService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`REAP_BATCH_LIMIT` bounds how long one pass holds its transaction open. Patched down to
    2 rather than seeding 101 rows: the number is not the property under test, "a full batch
    reports it and the rest survives to the next pass" is."""
    await _assert_no_foreign_stuck_runs(websites_db)
    monkeypatch.setattr("app.features.runs.service.REAP_BATCH_LIMIT", 2)

    run_ids = [(await _seed_orphan(websites_db, f"batch-{i}"))[1] for i in range(3)]

    first = await _reap(run_service, _RecordingQueuePool())
    assert first.examined == 2, "the cap bounds the batch"
    assert first.limit_reached is True, "and the pass says so, so the log can warn"

    # Every row is still `pending`, including the one the cap skipped — nothing was lost.
    for run_id in run_ids:
        assert (await _row(websites_db, run_id))["status"] == "pending"


def test_the_batch_limit_is_not_unbounded() -> None:
    """A guard against someone "fixing" a full-batch warning by removing the cap. An unbounded
    pass would hold row locks across an arbitrary number of rows in a table two other writers
    are actively using."""
    assert 0 < REAP_BATCH_LIMIT <= 1000


# -----------------------------------------------------------------------------------------
# 7. Concurrency. Both of these were mutation-tested — see the module docstring.
# -----------------------------------------------------------------------------------------


# No longer the overlap window — `_LockRendezvous` above makes overlap deterministic
# regardless of batch size (PER-198). What a batch of 25 still buys: it makes the loser's
# `SKIP LOCKED` skip observable across many rows rather than a coin-flip a single row would be,
# and it gives `_LockRendezvous.locked_ids` two non-trivial sets to assert are disjoint.
# `tests/test_schedule_tick.py` learned the ORIGINAL reason for a batch the hard way — its
# single-row version of the analogous tick test passed with all locking deleted, because the
# second coroutine was still opening a TCP connection while the first committed — but that
# specific failure mode is exactly what the rendezvous below now rules out directly, rather
# than by outrunning it with enough rows.
_CONCURRENT_BATCH = 25


async def test_two_concurrent_reapers_enqueue_each_orphan_exactly_once(
    websites_db: Pool, run_service: RunService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`FOR UPDATE SKIP LOCKED`, driven the way it will actually be exercised: two overlapping
    passes against the real pool — with the overlap made DETERMINISTIC by `_LockRendezvous`
    rather than left to the event loop's mood (PER-198; see the module docstring's "root
    cause" section for how the old, hope-based version of this test produced the exact
    `assert 2 == 1` this one guards against, with `FOR UPDATE SKIP LOCKED` fully intact).

    **Centred on the ORPHAN sweep, and that is the whole design of the test.** The obvious
    version — seed stuck `processing` runs, gather two reapers, assert each was rescued once —
    cannot fail. Every write in `runs_writer.py` names the status it may leave, so a second
    reaper acting on the same row issues an `UPDATE` that matches zero rows and correctly does
    nothing. The DB state is identical with locking and without it, which makes that version
    of the test theater no matter how many rows it seeds.

    Orphans have no such guard, because sweeping one writes nothing at all: a second reaper
    that reads the same `pending` rows enqueues the same jobs again. So the observable
    property is "exactly one enqueue per orphan", and it goes red for BOTH mutations —
    deleting `SKIP LOCKED` (the loser blocks, then — once `_LockRendezvous`'s timeout releases
    the winner and it commits — re-reads rows that are still `pending` and still old, and
    enqueues all of them again) and deleting `FOR UPDATE SKIP LOCKED` outright (the loser never
    blocks and does the same thing sooner, provably overlapping the winner's open transaction).
    See the module docstring's mutation-testing section for the actual, measured matrix.

    Stuck `processing` rows are seeded alongside anyway, and their final states asserted, so
    that the "no run is double-processed" claim covers both classes even though only one of
    them can prove the lock.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    orphan_ids = [
        (await _seed_orphan(websites_db, f"concurrent-orphan-{i}"))[1]
        for i in range(_CONCURRENT_BATCH)
    ]
    stuck_ids = [
        (await _seed_stuck(websites_db, f"concurrent-stuck-{i}", attempts=1))[1] for i in range(5)
    ]

    # Warm the pool: the rendezvous below still has to fit reaper 2's connection acquisition,
    # `BEGIN`, and one `SELECT` inside `_OVERLAP_TIMEOUT_SECONDS` after reaper 1's
    # `lock_reapable` returns. Without this, `pool.acquire()` opening a brand-new Postgres
    # connection — several milliseconds of TCP and auth — eats into that budget for no reason.
    # It no longer decides WHETHER the two passes overlap (the rendezvous below guarantees
    # that regardless), only how much of the timeout's headroom setup costs rather than margin.
    warm = await asyncio.gather(*(websites_db.acquire() for _ in range(4)))
    await asyncio.gather(*(websites_db.release(connection) for connection in warm))

    queues = [_RecordingQueuePool(), _RecordingQueuePool()]

    rendezvous = _LockRendezvous()
    rendezvous.install(monkeypatch)

    async def _second_reaper() -> Any:
        # Do not even start reaper 2 until reaper 1's `lock_reapable` has returned — reaper 1's
        # transaction is open and its locks are taken at that point, but nothing is committed.
        await asyncio.wait_for(rendezvous.first_locked.wait(), timeout=_OVERLAP_TIMEOUT_SECONDS)
        return await _reap(run_service, queues[1])

    await asyncio.gather(_reap(run_service, queues[0]), _second_reaper())

    assert rendezvous.calls == 2, (
        "lock_reapable was called a different number of times than this rendezvous assumes — "
        "a refactor changed how many times one reaper pass calls it, and the rendezvous below "
        "needs updating to match"
    )
    assert not rendezvous.timed_out, (
        f"reaper 2 did not complete its lock_reapable within {_OVERLAP_TIMEOUT_SECONDS}s while "
        "reaper 1 held its transaction open, so the two passes never overlapped and the count "
        "assertion below would be meaningless either way. Two causes, in order of likelihood: "
        "(1) SKIP LOCKED is missing from _LOCK_REAPABLE, so reaper 2 is BLOCKED on reaper 1's "
        "row locks — a real regression, and the one "
        "test_skip_locked_lets_the_reaper_past_a_row_someone_else_is_holding is built to name; "
        "(2) the handoff in this test broke. Check _LOCK_REAPABLE first."
    )
    # Lock-level disjointness, robust to `REAP_BATCH_LIMIT`: reaper 1 takes its locks first and
    # holds them for the rendezvous's whole duration, so reaper 2 cannot physically lock a row
    # reaper 1 is holding — even if reaper 1 filled its batch and reaper 2 legitimately picked
    # up the remainder, the two sets are still disjoint.
    assert rendezvous.locked_ids[1].isdisjoint(rendezvous.locked_ids[2]), (
        "reaper 2 locked one or more rows reaper 1 was already holding — "
        "FOR UPDATE SKIP LOCKED is not holding"
    )

    # **Split by the DIRECTION of the failure — the message may only accuse `FOR UPDATE SKIP
    # LOCKED` when the count is actually TOO HIGH.** A count of 0 means the orphan was
    # enqueued by NOBODY, which the lock cannot explain — `SKIP LOCKED` stops a row from being
    # taken twice, it has no opinion about a row nobody took. The likeliest cause of `0` is
    # foreign `pending`/`processing` rows in TEST_DATABASE_URL (the reaper is unscoped by
    # design — see the module docstring's global-state guard) pushing this pass's batch past
    # `REAP_BATCH_LIMIT`, so this row was never even examined; a second, rarer cause is another
    # process mutating or deleting rows mid-test. Neither implicates the lock, so accusing it
    # here would be exactly the false-accusation risk this whole rewrite exists to remove —
    # just aimed the opposite direction from the `count > 1` case.
    enqueued = queues[0].run_ids() + queues[1].run_ids()
    for run_id in orphan_ids:
        count = enqueued.count(str(run_id))
        assert count <= 1, (
            f"orphan {run_id} was enqueued {count} times, not 1 — two reapers both enqueued "
            "this orphan. The two passes provably overlapped — reaper 2's lock_reapable "
            "completed while reaper 1 held its transaction open (see the rendezvous "
            "assertions above) — so this is FOR UPDATE SKIP LOCKED not holding, not a "
            "scheduling accident."
        )
        assert count == 1, (
            f"orphan {run_id} was enqueued {count} times, not 1 — it was never enqueued by "
            "either pass. This is NOT a locking failure: FOR UPDATE SKIP LOCKED only stops a "
            "row from being taken twice, it cannot explain a row nobody took. Likeliest cause: "
            f"foreign reapable rows in TEST_DATABASE_URL pushed this pass's batch past "
            f"REAP_BATCH_LIMIT={REAP_BATCH_LIMIT} before it reached this row — check for a "
            "foreign pytest process against the same database before suspecting the reaper."
        )

    # Final DATABASE state for the stuck half: each rescued exactly once, and none left
    # `processing`. This cannot distinguish the mutations (see the module docstring) but it is
    # the claim the ticket makes, so it is asserted rather than assumed. Same direction-split
    # as the orphan loop above, and the same reason: a count of 0 here is exactly as likely to
    # be a foreign-row batch overrun as a real bug, and the message must not conflate the two.
    for run_id in stuck_ids:
        row = await _row(websites_db, run_id)
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        stuck_count = enqueued.count(str(run_id))
        assert stuck_count <= 1, (
            f"stuck run {run_id} was enqueued {stuck_count} times, not 1 — double-enqueued. "
            "The `processing` rows in this batch cannot distinguish either mutation on their "
            "own (see the module docstring), but a count above 1 is still worth knowing about."
        )
        assert stuck_count == 1, (
            f"stuck run {run_id} was enqueued {stuck_count} times, not 1 — never enqueued by "
            f"either pass. As with the orphans above, check for foreign reapable rows pushing "
            f"this batch past REAP_BATCH_LIMIT={REAP_BATCH_LIMIT} (or a foreign pytest process "
            "against the same database) before suspecting the reaper itself."
        )


async def test_skip_locked_lets_the_reaper_past_a_row_someone_else_is_holding(
    websites_db: Pool, run_service: RunService
) -> None:
    """Holds a plain `FOR UPDATE` lock on one stuck run from a SEPARATE connection, then runs
    one real pass and requires it to finish within `timeout=5` and to have rescued the other.

    **This is the mutation the test above cannot see, and the property the reaper actually
    needs in production.** The reaper scans a table that crawl workers lock rows in constantly
    — `claim_pending`, `mark_processing_completed`, `mark_processing_failed` all take row
    locks. Without `SKIP LOCKED`, `lock_reapable` queues behind whichever of them it happens
    to meet and holds a pooled connection for the duration. A regression here therefore shows
    up as `asyncio.wait_for` TIMING OUT, not as a wrong count, which is why it needs a test of
    its own with a deadline in it.
    """
    await _assert_no_foreign_stuck_runs(websites_db)
    _held_website, held_run = await _seed_stuck(websites_db, "held", attempts=1)
    _free_website, free_run = await _seed_stuck(websites_db, "free", attempts=1)

    holder = await websites_db.acquire()
    tx = holder.transaction()
    await tx.start()
    try:
        await holder.fetchval("SELECT id FROM runs WHERE id = $1 FOR UPDATE", held_run)

        queue = _RecordingQueuePool()
        summary = await asyncio.wait_for(_reap(run_service, queue), timeout=5)

        # The unlocked row was rescued promptly...
        assert (await _row(websites_db, free_run))["status"] == "pending"
        assert queue.run_ids().count(str(free_run)) == 1
        assert str(held_run) not in queue.run_ids()
        assert summary.requeued == 1
    finally:
        await tx.rollback()
        await websites_db.release(holder)

    # ...and the held one was skipped, not lost: it is still `processing`, and the next pass
    # picks it up once the lock is gone. Asserted after the lock is released, which is also
    # what proves the row was never modified while it was held.
    assert (await _row(websites_db, held_run))["status"] == "processing"

    second = await _reap(run_service, _RecordingQueuePool())
    assert second.requeued == 1
    assert (await _row(websites_db, held_run))["status"] == "pending"


async def test_lock_reapable_refuses_to_run_on_a_pool(websites_db: Pool) -> None:
    """`FOR UPDATE` outside a transaction takes its locks and hands the connection — and the
    locks with it — straight back to the pool before the caller sees a row. Guarded loudly
    rather than left to be discovered as a flaky concurrency test, exactly as
    `SchedulesReader.lock_due` guards the same mistake."""
    with pytest.raises(RuntimeError, match="Pool"):
        await RunsReader(websites_db).lock_reapable(
            stuck_before=_STUCK_BEFORE, orphaned_before=_ORPHANED_BEFORE, limit=10
        )


# -----------------------------------------------------------------------------------------
# 8. The pass reports itself, every time.
# -----------------------------------------------------------------------------------------


async def test_an_empty_pass_is_not_special_cased(
    websites_db: Pool, run_service: RunService
) -> None:
    """The overwhelmingly common case, and it must go through the same code as every other
    pass — a job that only logs when it finds something is indistinguishable from a job that
    stopped firing.

    `requeued` and `failed` are the counts pinned at zero rather than `examined`: this suite
    seeds nothing here, but a developer's database may hold an unrelated orphaned `pending`
    row, and the reaper reading one is correct behaviour rather than a failure of this test.
    Nothing was WRITTEN is the claim, and it is the claim that matters.
    """
    await _assert_no_foreign_stuck_runs(websites_db)

    summary = await _reap(run_service, _RecordingQueuePool())

    assert summary.requeued == 0
    assert summary.failed == 0
    assert summary.limit_reached is False


async def test_the_reaper_tick_job_binds_one_tick_id_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job wrapper, with the service stubbed out: a failing pass is logged and answered
    with a string rather than raised at arq, matching `schedule_tick`'s contract and its
    `max_tries=1` registration."""

    class _Service:
        async def reap_stuck_runs(self, queue: object, **kwargs: object) -> object:
            raise RuntimeError("Postgres went away")

    monkeypatch.setattr(jobs, "build_run_service", lambda *args: _Service())

    assert await jobs.reaper_tick({"db_pool": None, "redis": None}) == "failed"


async def test_the_reaper_tick_job_passes_thresholds_derived_from_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job resolves `app/worker/policy.py`'s durations into absolute instants and hands
    those to the service — which is what keeps `app/features/` free of any import from
    `app/worker/` (ARCHITECTURE.md §3.3).

    Asserted as an ORDERING between the two instants and `now`, not as exact values: pinning
    the arithmetic would just restate `policy.py` in a second place, while the ordering is the
    property that actually has to hold (a stuck run gets a longer grace than an orphan).
    """
    captured: dict[str, Any] = {}

    class _Service:
        async def reap_stuck_runs(self, queue: object, **kwargs: object) -> object:
            captured.update(kwargs)
            return _Summary()

    class _Summary:
        examined = requeued = orphans_swept = failed = 0
        enqueued = already_queued = enqueue_failures = 0

    monkeypatch.setattr(jobs, "build_run_service", lambda *args: _Service())

    await jobs.reaper_tick({"db_pool": None, "redis": None})

    now = datetime.now(UTC)
    assert captured["max_attempts"] == policy.MAX_ATTEMPTS
    assert captured["stuck_before"] < captured["orphaned_before"] < now
    assert captured["stuck_before"] < now - timedelta(seconds=policy.JOB_TIMEOUT_SECONDS)


def test_reaper_tick_is_not_registered_as_an_enqueueable_function() -> None:
    """It is a cron job, not something anything enqueues. Registering it in `functions` too
    would make it callable by name from the API's queue pool, which is a capability nothing
    should have — the reaper's cadence is the only thing bounding how often it takes locks on
    a table two other writers are using."""
    assert all(function.__name__ != "reaper_tick" for function in WorkerSettings.functions)
