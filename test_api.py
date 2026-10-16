"""Integration tests against real Postgres and Redis."""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import cache, db, worker
from app import queue as jobq
from app.main import app

client = TestClient(app)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def make_png(width=800, height=600):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def submit(body=None, width=200, height=200, content_type="image/png"):
    body = make_png() if body is None else body
    r = client.post(f"/jobs?width={width}&height={height}&filename=t.png",
                    content=body, headers={"content-type": content_type})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def run_worker_once():
    job_id = jobq.claim(1)
    assert job_id is not None, "nothing was on the queue"
    worker.run_one(job_id)
    return job_id


def simulate_dead_worker(job_id, attempts=0, with_lease=False):
    """Claim the job, mark it processing, and 'crash'."""
    assert jobq.claim(1) == job_id
    db.query("UPDATE jobs SET state='processing', attempts=%s,"
             " leased_at=now() - interval '1 minute' WHERE id=%s",
             (attempts, job_id), fetch=False)
    if with_lease:
        jobq.take_lease(job_id)


# ---- health & reads --------------------------------------------------------

def test_health_reports_both_backends():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "postgres": True, "redis": True}


def test_list_and_stats_include_seed_data():
    body = client.get("/jobs").json()
    assert body["summary"]["total"] >= 5
    assert body["summary"]["done"] >= 3
    assert body["summary"]["dead"] >= 2
    assert "waiting" in body["queue"]
    stats = client.get("/stats").json()
    assert "slowest" in stats


def test_list_rejects_bad_state_and_limit():
    assert client.get("/jobs?state=bogus").status_code == 400
    assert client.get("/jobs?limit=0").status_code == 400


def test_unknown_job_is_404():
    assert client.get("/jobs/999999").status_code == 404
    assert client.get("/jobs/999999/result").status_code == 404


def test_seeded_result_is_a_png():
    r = client.get("/jobs/1/result")
    assert r.status_code == 200
    assert r.content.startswith(PNG_MAGIC)


# ---- submit & validation ---------------------------------------------------

def test_submit_returns_202_and_queues_the_job():
    job_id = submit()
    assert client.get(f"/jobs/{job_id}").json()["state"] == "queued"
    assert cache.client().llen(jobq.QUEUE) == 1


def test_submit_rejects_empty_body():
    r = client.post("/jobs", content=b"", headers={"content-type": "image/png"})
    assert r.status_code == 400


def test_submit_rejects_non_image_content_type():
    r = client.post("/jobs", content=b"hello", headers={"content-type": "text/plain"})
    assert r.status_code == 400


def test_submit_rejects_bad_box():
    r = client.post("/jobs?width=0&height=10", content=make_png(),
                    headers={"content-type": "image/png"})
    assert r.status_code == 400


# ---- the worker ------------------------------------------------------------

def test_worker_resizes_and_stores_result():
    job_id = submit(width=200, height=200)
    assert run_worker_once() == job_id

    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "done"
    assert body["result"] == {"width": 200, "height": 150}
    assert body["source"] == {"width": 800, "height": 600}

    r = client.get(f"/jobs/{job_id}/result")
    assert r.status_code == 200
    assert r.content.startswith(PNG_MAGIC)
    assert r.headers["X-Image-Width"] == "200"
    assert cache.client().llen(jobq.PROCESSING) == 0


def test_result_of_unfinished_job_is_409():
    job_id = submit()
    assert client.get(f"/jobs/{job_id}/result").status_code == 409


def test_bad_image_fails_and_is_scheduled_for_retry():
    job_id = submit(body=b"this is not an image")
    run_worker_once()

    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "failed"
    assert body["attempts"] == 1
    assert body["last_error"]
    assert cache.client().zscore(jobq.RETRY, str(job_id)) is not None
    assert cache.client().llen(jobq.PROCESSING) == 0


def test_manual_retry_requeues_a_failed_job():
    job_id = submit(body=b"garbage")
    run_worker_once()
    r = client.post(f"/jobs/{job_id}/retry")
    assert r.status_code == 200
    assert r.json()["state"] == "queued"
    assert cache.client().llen(jobq.QUEUE) == 1
    assert cache.client().zscore(jobq.RETRY, str(job_id)) is None


def test_done_and_dead_jobs_cannot_be_retried():
    assert client.post("/jobs/1/retry").status_code == 409   # done
    assert client.post("/jobs/4/retry").status_code == 409   # dead


def test_job_is_buried_after_three_attempts():
    job_id = submit(body=b"garbage")
    for _ in range(3):
        db.query("UPDATE jobs SET state='queued' WHERE id=%s", (job_id,), fetch=False)
        cache.client().delete(jobq.QUEUE)
        jobq.push(job_id)
        run_worker_once()
    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "dead"
    assert body["attempts"] == 3


# ---- crash recovery --------------------------------------------------------

def test_reap_requeues_job_whose_lease_expired():
    job_id = submit()
    simulate_dead_worker(job_id, attempts=0, with_lease=False)

    r = client.post("/admin/reap")
    assert r.status_code == 200
    actions = {a["job_id"]: a["action"] for a in r.json()["actions"]}
    assert actions[job_id] == "requeue"

    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "queued"
    assert body["attempts"] == 1
    c = cache.client()
    assert c.llen(jobq.PROCESSING) == 0
    assert c.lrange(jobq.QUEUE, 0, -1) == [str(job_id)]


def test_reap_buries_job_that_is_out_of_attempts():
    job_id = submit()
    simulate_dead_worker(job_id, attempts=2)
    client.post("/admin/reap")
    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "dead"
    assert body["attempts"] == 3
    assert cache.client().llen(jobq.QUEUE) == 0


def test_reap_leaves_a_live_lease_alone():
    job_id = submit()
    simulate_dead_worker(job_id, attempts=0, with_lease=True)
    r = client.post("/admin/reap")
    assert r.json()["actions"] == []
    assert client.get(f"/jobs/{job_id}").json()["state"] == "processing"
    assert cache.client().llen(jobq.PROCESSING) == 1


def test_worker_reaper_recovers_a_crashed_job():
    job_id = submit()
    simulate_dead_worker(job_id, attempts=0)
    worker.reap()
    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "queued"
    assert body["attempts"] == 1


def test_reconcile_restores_queued_rows_missing_from_redis():
    row = db.one("INSERT INTO jobs (state, filename, src_bytes, src_data)"
                 " VALUES ('queued','x.png',3,%s) RETURNING id", (b"abc",))
    assert cache.client().llen(jobq.QUEUE) == 0
    worker.reconcile()
    assert cache.client().lrange(jobq.QUEUE, 0, -1) == [str(row["id"])]


@pytest.mark.parametrize("path", ["/health", "/jobs", "/stats"])
def test_read_endpoints_return_200(path):
    assert client.get(path).status_code == 200
