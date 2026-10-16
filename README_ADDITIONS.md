## Running it

```bash
docker compose up --build
curl localhost:8080/health          # {"status":"ok","postgres":true,"redis":true}
curl -s --data-binary @photo.png -H 'content-type: image/png' \
     'localhost:8080/jobs?width=200&height=200&filename=photo.png'
```

Services: `postgres`, `redis`, `migrate` (one-shot, applies `migrations/*.sql`
in filename order), `web` (API, port 8080) and `worker` (same image, command
`python -m app.worker`, no ports).

Tests: `pip install -r requirements-dev.txt && pytest --cov=app --cov-report=term-missing`
(integration tests need Postgres and Redis on localhost, or set `DATABASE_URL` / `REDIS_URL`).

CI (`.circleci/config.yml`): lint -> unit tests -> integration tests -> secret
scan -> image build -> push `:1.0` to Docker Hub (main branch only; needs
`DOCKERHUB_USERNAME` and `DOCKERHUB_PASSWORD` set in the CircleCI project).

## The hard part: surviving a crashed worker

**Proof.** Set `SLOW_MS: "20000"` on the worker, `docker compose up -d`, submit a
job, then `docker compose kill -s KILL worker`. `redis-cli lrange imgq:processing 0 -1`
still shows the id. `redis-cli ttl imgq:lease:<id>` counts down and the key
disappears by itself. `curl -X POST localhost:8080/admin/reap` (or restarting the
worker, whose loop reaps) moves the job back to `queued` with `attempts`
incremented by one.

**Why BLMOVE rather than LPOP?** LPOP removes the id from Redis and hands it to the
worker, so for a moment the id exists only in that process's memory. If the worker
dies in that window, nothing anywhere records that the job was ever taken. The
window is only a network round trip plus the next write, which is milliseconds, but
its width is not the point: the failure is silent and scales with throughput. BLMOVE
takes the id off `imgq:queue` and appends it to `imgq:processing` in one atomic
operation, so the id is always in at least one list. The window is closed, not narrowed.

**Why a lease with a TTL rather than the worker cleaning up after itself?** A
`kill -9`, OOM kill, node loss or power cut runs no cleanup code, so a design that
relies on the worker's promise fails in exactly the case it exists for. A TTL puts
the timer in Redis, which does not depend on the dead process. A live worker is
"proven" by the key existing; a dead one is proven by it vanishing. The TTL also
bounds how long a stuck-but-alive worker can hold a job.

**Why must attempts go up on a reclaim?** Reclaims are failures too. A "poison" job
(an image that exhausts memory and gets the worker OOM-killed) would otherwise
be requeued, picked up, kill the next worker, be reclaimed, and repeat forever,
taking workers down one after another with no error ever recorded. Counting the
reclaim means it reaches `MAX_ATTEMPTS` and is buried as `dead`, where a human can
see it (`reclaim` returns `bury` once attempts are used up).

**Why is running a job twice the right trade?** This is at-least-once delivery. A
worker that finishes the resize and dies before writing `done` has its job
reclaimed and resized again. The alternative, at-most-once, loses work on every
crash, and a lost job is invisible to the user, whereas a duplicate is not.
Here a duplicate is harmless because the work is idempotent: the same input and box
always produce the same PNG, and writing it twice overwrites the same row.

**What would exactly-once take?** Redis and Postgres cannot commit together, so the
"claim" and the "result" can never be one atomic step across them. To get there:
(1) move the queue into Postgres (`SELECT ... FOR UPDATE SKIP LOCKED`) so claiming a
job and recording its result are in one transaction; (2) add a fencing token (a
`lease_token` column checked in `UPDATE ... WHERE id=? AND lease_token=?`) so a
zombie worker that wakes up after its lease expired cannot overwrite a newer
worker's result; (3) for any external side effect (email, webhook), an idempotency
key so the receiver ignores the second call. Even then, "exactly once" is really
"at least once delivery plus idempotent effects".
