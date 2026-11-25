# backend/app/middleware.py
import time
import os
import uuid
from typing import Callable
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from logger import logger, log_request, log_error, set_correlation_id, reset_correlation_id
from exceptions import AIPortfolioException, RateLimitError

class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all HTTP requests"""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        token = set_correlation_id(correlation_id)
        
        # Log request
        try:
            logger.info("request.started", extra={"event": "request.started", "method": request.method, "path": request.url.path})
            response = await call_next(request)
            duration_ms = (time.time() - start_time) * 1000
            log_request(request.method, request.url.path, response.status_code, duration_ms)
            response.headers["X-Process-Time"] = f"{duration_ms:.2f}ms"
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            reset_correlation_id(token)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to add security headers"""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        
        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        
        return response

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware to implement token-bucket rate limiting.
    Default: 6000 requests per minute per IP.
    """
    
    def __init__(self, app, requests_per_minute: int = 6000):
        super().__init__(app)
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.bucket_capacity = float(self.requests_per_minute)
        self.refill_rate_per_second = self.bucket_capacity / 60.0
        self.cleanup_interval_seconds = float(os.getenv("RATE_LIMIT_CLEANUP_INTERVAL_SECONDS", "30"))
        self.bucket_ttl_seconds = float(os.getenv("RATE_LIMIT_BUCKET_TTL_SECONDS", "300"))
        self.max_tracked_clients = max(1, int(os.getenv("RATE_LIMIT_MAX_CLIENTS", "10000")))
        self.buckets = {}
        self.last_cleanup_monotonic = time.monotonic()
        self.trust_forwarded_for = str(os.getenv("TRUST_X_FORWARDED_FOR", "false")).strip().lower() in {
            "1", "true", "yes", "on"
        }
    
    def _cleanup_buckets(self, now_monotonic: float):
        """Drop stale buckets and enforce max tracked clients bound."""
        if (now_monotonic - self.last_cleanup_monotonic) < self.cleanup_interval_seconds:
            return

        stale_threshold = now_monotonic - self.bucket_ttl_seconds
        for ip, bucket in list(self.buckets.items()):
            if float(bucket.get("last_seen_monotonic", 0.0)) < stale_threshold:
                del self.buckets[ip]

        if len(self.buckets) > self.max_tracked_clients:
            overflow = len(self.buckets) - self.max_tracked_clients
            oldest_ips = sorted(
                self.buckets,
                key=lambda ip_key: float(self.buckets[ip_key].get("last_seen_monotonic", 0.0)),
            )[:overflow]
            for ip in oldest_ips:
                self.buckets.pop(ip, None)

        self.last_cleanup_monotonic = now_monotonic

    def _get_or_create_bucket(self, client_ip: str, now_monotonic: float) -> dict:
        bucket = self.buckets.get(client_ip)
        if bucket is None:
            bucket = {
                "tokens": self.bucket_capacity,
                "last_refill_monotonic": now_monotonic,
                "last_seen_monotonic": now_monotonic,
            }
            self.buckets[client_ip] = bucket
        return bucket

    def _consume_token(self, client_ip: str, now_monotonic: float) -> tuple[bool, int, int]:
        self._cleanup_buckets(now_monotonic)
        bucket = self._get_or_create_bucket(client_ip, now_monotonic)

        elapsed = max(0.0, now_monotonic - float(bucket["last_refill_monotonic"]))
        if elapsed > 0.0:
            bucket["tokens"] = min(
                self.bucket_capacity,
                float(bucket["tokens"]) + elapsed * self.refill_rate_per_second,
            )
            bucket["last_refill_monotonic"] = now_monotonic

        bucket["last_seen_monotonic"] = now_monotonic

        if float(bucket["tokens"]) < 1.0:
            wait_seconds = max(0.0, (1.0 - float(bucket["tokens"])) / self.refill_rate_per_second)
            reset_epoch = int(time.time() + wait_seconds)
            return False, 0, reset_epoch

        bucket["tokens"] = max(0.0, float(bucket["tokens"]) - 1.0)
        remaining = max(0, int(float(bucket["tokens"])))
        return True, remaining, int(time.time())
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded and self.trust_forwarded_for:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip rate limiting for health check
        if request.url.path in {"/health", "/api/health"}:
            return await call_next(request)
        
        client_ip = self._get_client_ip(request)
        now_monotonic = time.monotonic()

        allowed, remaining, reset_epoch = self._consume_token(client_ip, now_monotonic)

        if not allowed:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            raise RateLimitError(
                message=f"Rate limit exceeded. Maximum {self.requests_per_minute} requests per minute.",
                details={
                    "client_ip": client_ip,
                    "limit": self.requests_per_minute,
                    "remaining": remaining,
                    "reset_epoch": reset_epoch,
                }
            )
        
        response = await call_next(request)
        
        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_epoch)
        
        return response

class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Middleware to handle exceptions globally"""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except AIPortfolioException as e:
            log_error(e, context={
                "method": request.method,
                "path": request.url.path,
                "status_code": e.status_code
            })
            return JSONResponse(
                status_code=e.status_code,
                content={
                    "error": e.message,
                    "details": e.details,
                    "status_code": e.status_code
                }
            )
        except Exception as e:
            log_error(e, context={
                "method": request.method,
                "path": request.url.path
            })
            include_error_details = str(os.getenv("EXPOSE_INTERNAL_ERRORS", "false")).strip().lower() in {
                "1", "true", "yes", "on"
            }
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Internal server error",
                    "details": {"message": str(e)} if include_error_details else {},
                    "status_code": 500
                }
            )
