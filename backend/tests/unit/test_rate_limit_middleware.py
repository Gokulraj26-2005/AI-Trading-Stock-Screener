import os
import sys
import asyncio

import pytest
from fastapi import Request, Response

# Add parent directory to path for imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import middleware as middleware_module  # noqa: E402
from exceptions import RateLimitError  # noqa: E402


async def _dummy_app(scope, receive, send):
    return None


def _build_request(path: str = "/probe", ip: str = "127.0.0.1", headers: dict[str, str] | None = None) -> Request:
    header_items = []
    for key, value in (headers or {}).items():
        header_items.append((key.lower().encode("utf-8"), value.encode("utf-8")))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": header_items,
        "client": (ip, 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


async def _ok_response(_request: Request) -> Response:
    return Response(content="ok", media_type="text/plain")


def test_consume_token_blocks_after_capacity():
    limiter = middleware_module.RateLimitMiddleware(_dummy_app, requests_per_minute=2)

    first_allowed, first_remaining, _ = limiter._consume_token("1.1.1.1", now_monotonic=0.0)
    second_allowed, second_remaining, _ = limiter._consume_token("1.1.1.1", now_monotonic=0.0)
    third_allowed, third_remaining, _ = limiter._consume_token("1.1.1.1", now_monotonic=0.0)

    assert first_allowed is True
    assert second_allowed is True
    assert third_allowed is False
    assert first_remaining == 1
    assert second_remaining == 0
    assert third_remaining == 0


def test_consume_token_refills_after_elapsed_time():
    limiter = middleware_module.RateLimitMiddleware(_dummy_app, requests_per_minute=1)

    first_allowed, _, _ = limiter._consume_token("2.2.2.2", now_monotonic=0.0)
    second_allowed, _, _ = limiter._consume_token("2.2.2.2", now_monotonic=0.0)
    third_allowed, third_remaining, _ = limiter._consume_token("2.2.2.2", now_monotonic=61.0)

    assert first_allowed is True
    assert second_allowed is False
    assert third_allowed is True
    assert third_remaining == 0


def test_cleanup_respects_max_tracked_clients():
    limiter = middleware_module.RateLimitMiddleware(_dummy_app, requests_per_minute=10)
    limiter.max_tracked_clients = 2
    limiter.cleanup_interval_seconds = 0.0
    limiter.bucket_ttl_seconds = 10_000.0
    limiter.last_cleanup_monotonic = 0.0

    limiter.buckets = {
        "1.1.1.1": {"tokens": 1.0, "last_refill_monotonic": 10.0, "last_seen_monotonic": 10.0},
        "2.2.2.2": {"tokens": 1.0, "last_refill_monotonic": 20.0, "last_seen_monotonic": 20.0},
        "3.3.3.3": {"tokens": 1.0, "last_refill_monotonic": 30.0, "last_seen_monotonic": 30.0},
    }

    limiter._cleanup_buckets(now_monotonic=100.0)

    assert len(limiter.buckets) == 2
    assert "1.1.1.1" not in limiter.buckets


def test_dispatch_adds_rate_limit_headers():
    limiter = middleware_module.RateLimitMiddleware(_dummy_app, requests_per_minute=2)
    request = _build_request(path="/api/probe")

    response = asyncio.run(limiter.dispatch(request, _ok_response))

    assert response.status_code == 200
    assert response.headers.get("X-RateLimit-Limit") == "2"
    assert response.headers.get("X-RateLimit-Remaining") == "1"


def test_dispatch_raises_rate_limit_error_when_exceeded():
    limiter = middleware_module.RateLimitMiddleware(_dummy_app, requests_per_minute=1)
    request = _build_request(path="/api/probe")

    asyncio.run(limiter.dispatch(request, _ok_response))

    with pytest.raises(RateLimitError):
        asyncio.run(limiter.dispatch(request, _ok_response))


def test_dispatch_skips_api_health_path():
    limiter = middleware_module.RateLimitMiddleware(_dummy_app, requests_per_minute=1)
    request = _build_request(path="/api/health")

    first_response = asyncio.run(limiter.dispatch(request, _ok_response))
    second_response = asyncio.run(limiter.dispatch(request, _ok_response))

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.headers.get("X-RateLimit-Limit") is None
