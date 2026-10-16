"""Unit tests for app/jobs.py. Pure logic: no database, no network, no clock."""
import pytest

from app import jobs
from app.jobs import JobError

ALL = jobs.STATES


# ---- state machine ---------------------------------------------------------

def test_queued_can_start_processing():
    assert jobs.can_transition("queued", "processing") is True


def test_done_cannot_be_processed_again():
    assert jobs.can_transition("done", "processing") is False


def test_processing_can_go_dead_on_last_attempt():
    assert jobs.can_transition("processing", "dead") is True


@pytest.mark.parametrize("terminal", ["done", "dead"])
@pytest.mark.parametrize("target", ALL)
def test_terminal_states_have_no_exits(terminal, target):
    assert jobs.can_transition(terminal, target) is False


def test_is_terminal():
    assert jobs.is_terminal("done") and jobs.is_terminal("dead")
    assert not jobs.is_terminal("queued")
    assert not jobs.is_terminal("failed")


def test_transition_returns_new_state():
    assert jobs.transition("queued", "processing") == "processing"


def test_transition_refuses_loudly_naming_both_states():
    with pytest.raises(JobError) as exc:
        jobs.transition("done", "processing")
    assert "done" in str(exc.value) and "processing" in str(exc.value)


@pytest.mark.parametrize("bad", ["bogus", "", "QUEUED"])
def test_unknown_state_is_an_error(bad):
    with pytest.raises(JobError):
        jobs.check_state(bad)
    with pytest.raises(JobError):
        jobs.can_transition(bad, "queued")
    with pytest.raises(JobError):
        jobs.can_transition("queued", bad)


# ---- retry / backoff -------------------------------------------------------

def test_should_retry_boundaries():
    assert jobs.should_retry(1) is True
    assert jobs.should_retry(2) is True
    assert jobs.should_retry(3) is False


def test_should_retry_rejects_negative():
    with pytest.raises(JobError):
        jobs.should_retry(-1)


def test_backoff_grows_then_caps():
    values = [jobs.backoff_seconds(n) for n in range(1, 10)]
    assert values[0] == 2.0 and values[0] > 0
    assert values[:5] == sorted(values[:5])
    assert values[-1] == jobs.BACKOFF_CAP
    assert max(values) == jobs.BACKOFF_CAP


def test_backoff_attempts_start_at_one():
    with pytest.raises(JobError):
        jobs.backoff_seconds(0)


def test_plan_after_first_failure_retries():
    plan = jobs.plan_after_failure(1, now=100.0)
    assert plan["state"] == "failed"
    assert plan["then"] == "queued"
    assert plan["retry_in"] == 2.0
    assert plan["retry_at"] == 102.0


def test_plan_after_last_failure_buries():
    plan = jobs.plan_after_failure(3, now=100.0)
    assert plan["state"] == "dead"
    assert plan["then"] is None
    assert plan["retry_at"] is None


# ---- leases ----------------------------------------------------------------

def test_lease_deadline():
    assert jobs.lease_deadline(100, 30) == 130.0


def test_lease_must_be_positive():
    with pytest.raises(JobError):
        jobs.lease_deadline(100, 0)


def test_lease_expired_is_exclusive_at_boundary():
    assert jobs.lease_expired(100, 130, 30) is True      # ends exactly now
    assert jobs.lease_expired(100, 129.999, 30) is False
    assert jobs.lease_expired(100, 200, 30) is True


# ---- reclaim: the heart of it ---------------------------------------------

def _job(state="processing", attempts=0, leased_at=100.0):
    return {"state": state, "attempts": attempts, "leased_at": leased_at}


def test_reclaim_requeues_lease_that_ended_one_second_ago():
    decision = jobs.reclaim(_job(attempts=0), now=131.0, lease_seconds=30)
    assert decision["action"] == "requeue"
    assert decision["attempts"] == 1


def test_reclaim_buries_when_attempts_are_used_up():
    decision = jobs.reclaim(_job(attempts=2), now=131.0, lease_seconds=30)
    assert decision["action"] == "bury"
    assert decision["attempts"] == 3


def test_reclaim_leaves_a_live_lease_alone():
    decision = jobs.reclaim(_job(), now=110.0, lease_seconds=30)
    assert decision["action"] == "leave"
    assert decision["expires_in"] == 20.0


def test_reclaim_leaves_a_finished_job_alone():
    decision = jobs.reclaim(_job(state="done"), now=9999.0, lease_seconds=30)
    assert decision["action"] == "leave"


@pytest.mark.parametrize("state", ["queued", "failed", "dead", "done"])
def test_reclaim_only_touches_processing_jobs(state):
    assert jobs.reclaim(_job(state=state), now=9999.0)["action"] == "leave"


def test_reclaim_requeues_when_no_lease_was_recorded():
    decision = jobs.reclaim(_job(leased_at=None, attempts=1), now=500.0)
    assert decision["action"] == "requeue"
    assert decision["attempts"] == 2


def test_reclaim_bumps_attempts_so_a_poison_job_eventually_dies():
    job = _job(attempts=0)
    actions = []
    for _ in range(5):
        decision = jobs.reclaim(job, now=1000.0, lease_seconds=30)
        actions.append(decision["action"])
        if decision["action"] != "requeue":
            break
        job = _job(attempts=decision["attempts"])
    assert actions == ["requeue", "requeue", "bury"]


# ---- target_size -----------------------------------------------------------

def test_target_size_preserves_aspect_ratio():
    assert jobs.target_size(4000, 3000, 320, 320) == (320, 240)


def test_target_size_never_upscales():
    assert jobs.target_size(100, 50, 320, 320) == (100, 50)


def test_target_size_image_inside_box_is_unchanged():
    assert jobs.target_size(320, 320, 320, 320) == (320, 320)


def test_target_size_never_returns_zero_edge():
    w, h = jobs.target_size(4000, 3, 320, 320)
    assert (w, h) == (320, 1)


def test_target_size_limited_by_height():
    assert jobs.target_size(1000, 2000, 500, 500) == (250, 500)


@pytest.mark.parametrize("args", [(0, 10, 10, 10), (10, 0, 10, 10),
                                  (10, 10, 0, 10), (10, 10, 10, 0)])
def test_target_size_rejects_zero(args):
    with pytest.raises(JobError):
        jobs.target_size(*args)


# ---- validation ------------------------------------------------------------

@pytest.mark.parametrize("size", [0, -5, None])
def test_validate_upload_rejects_empty(size):
    with pytest.raises(JobError):
        jobs.validate_upload(size)


def test_validate_upload_rejects_too_large():
    with pytest.raises(JobError):
        jobs.validate_upload(jobs.MAX_UPLOAD_BYTES + 1)


def test_validate_upload_accepts_limit_exactly():
    assert jobs.validate_upload(jobs.MAX_UPLOAD_BYTES) == jobs.MAX_UPLOAD_BYTES


def test_validate_upload_content_types():
    assert jobs.validate_upload(10, "image/png") == 10
    assert jobs.validate_upload(10, "application/octet-stream") == 10
    assert jobs.validate_upload(10, None) == 10
    with pytest.raises(JobError):
        jobs.validate_upload(10, "text/plain")


def test_validate_box():
    assert jobs.validate_box(320, 200) == (320, 200)
    with pytest.raises(JobError):
        jobs.validate_box(0, 200)
    with pytest.raises(JobError):
        jobs.validate_box(200, jobs.MAX_EDGE + 1)


# ---- progress / summarise --------------------------------------------------

def test_progress_values():
    assert jobs.progress("queued") == 0
    assert jobs.progress("processing") == 50
    assert jobs.progress("done") == 100
    with pytest.raises(JobError):
        jobs.progress("nope")


def test_summarise_counts_every_state_including_empty():
    out = jobs.summarise([{"state": "queued"}, {"state": "queued"},
                          {"state": "done"}, {"state": "failed"}])
    assert out["queued"] == 2
    assert out["done"] == 1
    assert out["processing"] == 0
    assert out["dead"] == 0
    assert out["total"] == 4
    assert out["outstanding"] == 3


def test_summarise_empty():
    out = jobs.summarise([])
    assert out["total"] == 0 and out["outstanding"] == 0
    assert set(ALL) <= set(out)
