"""E2E tests for the webhook ingestion pipeline.

Sends real HTTP requests (via ASGI in-process) to POST /webhook/github
with signed payloads.  All infrastructure adapters are fakes; only the
HTTP glue and use-case orchestration are exercised end-to-end.

Budget: tests must complete in < 5 s total (e2e layer is heavier than smoke).
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from openbot.entrypoints.api.app import app
from tests.e2e._assemble import E2EStack, make_issue_opened_payload


@pytest.fixture
async def client(e2e_stack: E2EStack) -> AsyncClient:
    """Return an AsyncClient wired to the real ASGI app with fakes injected."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.e2e
class TestWebhookIngestion:
    async def test_signed_issue_opened_returns_202(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """A valid signed issue.opened → HTTP 202 Accepted."""
        payload = make_issue_opened_payload(issue_number=1)
        body = json.dumps(payload).encode()
        async with client:
            response = await client.post(
                "/webhook/github",
                content=body,
                headers=e2e_stack.issue_opened_headers(body, delivery_id="e2e-001"),
            )
        assert response.status_code == 202

    async def test_unsigned_webhook_returns_401(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """A webhook with no signature header → HTTP 401 Unauthorized."""
        payload = make_issue_opened_payload(issue_number=2)
        body = json.dumps(payload).encode()
        async with client:
            response = await client.post(
                "/webhook/github",
                content=body,
                headers={
                    "x-github-event": "issues",
                    "x-github-delivery": "e2e-002",
                    "content-type": "application/json",
                    # No x-hub-signature-256
                },
            )
        assert response.status_code == 401

    async def test_wrong_signature_returns_401(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """A webhook with an incorrect signature → HTTP 401 Unauthorized."""
        payload = make_issue_opened_payload(issue_number=3)
        body = json.dumps(payload).encode()
        async with client:
            response = await client.post(
                "/webhook/github",
                content=body,
                headers={
                    "x-github-event": "issues",
                    "x-github-delivery": "e2e-003",
                    "x-hub-signature-256": "sha256=" + "0" * 64,
                    "content-type": "application/json",
                },
            )
        assert response.status_code == 401

    async def test_accepted_response_has_status_field(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """202 response body must include a 'status' field."""
        payload = make_issue_opened_payload(issue_number=4)
        body = json.dumps(payload).encode()
        async with client:
            response = await client.post(
                "/webhook/github",
                content=body,
                headers=e2e_stack.issue_opened_headers(body, delivery_id="e2e-004"),
            )
        assert response.status_code == 202
        assert "status" in response.json()

    async def test_accepted_response_has_kind_field(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """202 response body must include a 'kind' field."""
        payload = make_issue_opened_payload(issue_number=5)
        body = json.dumps(payload).encode()
        async with client:
            response = await client.post(
                "/webhook/github",
                content=body,
                headers=e2e_stack.issue_opened_headers(body, delivery_id="e2e-005"),
            )
        assert response.status_code == 202
        assert "kind" in response.json()

    async def test_duplicate_delivery_returns_202(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """Duplicate delivery_id → still HTTP 202 (GitHub expects idempotent 202)."""
        payload = make_issue_opened_payload(issue_number=6)
        body = json.dumps(payload).encode()
        # First delivery
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as first_client:
            await first_client.post(
                "/webhook/github",
                content=body,
                headers=e2e_stack.issue_opened_headers(body, delivery_id="e2e-006"),
            )
        # Second delivery with same delivery_id
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as second_client:
            response = await second_client.post(
                "/webhook/github",
                content=body,
                headers=e2e_stack.issue_opened_headers(body, delivery_id="e2e-006"),
            )
        assert response.status_code == 202
        assert response.json()["status"] == "duplicate"

    async def test_accepted_event_reaches_queue(
        self, client: AsyncClient, e2e_stack: E2EStack
    ) -> None:
        """A successful accepted webhook must enqueue exactly one TaskSpec."""
        payload = make_issue_opened_payload(issue_number=7)
        body = json.dumps(payload).encode()
        initial_count = len(e2e_stack.queue.task_specs)
        async with client:
            response = await client.post(
                "/webhook/github",
                content=body,
                headers=e2e_stack.issue_opened_headers(body, delivery_id="e2e-007"),
            )
        assert response.status_code == 202
        assert len(e2e_stack.queue.task_specs) == initial_count + 1
