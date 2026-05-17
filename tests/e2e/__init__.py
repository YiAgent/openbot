"""End-to-end demo tests — harness spec §7 acceptance scripts.

Each test in ``test_spec_demos.py`` corresponds to one demo from spec §7,
exercising the full preflight chain (and, for demo 9, the queue worker
recovery path) against an in-memory stack:

  - ``RecordingGitHubAdapter`` — records ``reply``/``label`` calls and
    returns canned responses for ``_authed_json`` reads (labels, comments).
  - ``fakeredis.aioredis.FakeRedis`` — drop-in Redis backend.
  - aiosqlite in-memory engine — drop-in Postgres backend.

No real HTTP, no real Redis, no real Postgres. The webhook signature
+ queue serialization path is covered by ``tests/test_webhook_endpoint.py``;
this suite focuses on **what happens inside ``run_dispatch``**.
"""
