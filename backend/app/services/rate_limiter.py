"""
Token Bucket Rate Limiter for Broker API Rate Limits
Implements strict rate limiting for broker endpoints:
- 10 req/s for most endpoints (100 tokens/10s bucket)
- 3 req/s for historical candles (3 tokens/1s bucket)
- 400 orders/min (order placement limit)
- 25 modifications/order
"""

import time
import logging
import asyncio
from typing import Dict, Optional
from dataclasses import dataclass
from threading import Lock

logger = logging.getLogger(__name__)

@dataclass(slots=True)
class RateLimitConfig:
    """Configuration for a rate limit bucket"""
    name: str
    tokens_per_interval: int
    interval_seconds: float
    max_tokens: Optional[int] = None  # Max burst size
    
    def __post_init__(self):
        if self.max_tokens is None:
            self.max_tokens = self.tokens_per_interval

class TokenBucket:
    """
    Token Bucket Rate Limiter
    Allows precise control over request rates
    """
    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.tokens = float(config.max_tokens or config.tokens_per_interval)
        self.last_refill_time = time.time()
        self.lock = Lock()
    
    def _refill(self):
        """Add tokens based on elapsed time"""
        now = time.time()
        elapsed = now - self.last_refill_time
        
        # Calculate tokens to add
        tokens_to_add = (elapsed / self.config.interval_seconds) * self.config.tokens_per_interval
        
        # Add tokens, capped at max
        self.tokens = min(
            self.config.max_tokens or self.config.tokens_per_interval,
            self.tokens + tokens_to_add
        )
        self.last_refill_time = now
    
    def try_consume(self, tokens: int = 1) -> bool:
        """
        Try to consume tokens. Returns True if successful, False if rate limited.
        """
        with self.lock:
            self._refill()
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False
    
    async def consume_or_wait(self, tokens: int = 1, max_wait: Optional[float] = None) -> bool:
        """
        Consume tokens, waiting if necessary.
        Returns False if max_wait exceeded.
        """
        start_time = time.time()
        
        while True:
            with self.lock:
                self._refill()
                
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
            
            if max_wait and (time.time() - start_time) > max_wait:
                return False
            
            # Calculate wait time until next token
            # Adaptive wait time proportional to rate instead of fixed spin-polling
            wait_time = max(0.005, (self.config.interval_seconds / max(self.config.tokens_per_interval, 1)) * 0.5)
            await asyncio.sleep(min(wait_time, 0.05))  # Sleep in non-blocking increments
    
    def get_available_tokens(self) -> float:
        """Get current token count"""
        with self.lock:
            self._refill()
            return self.tokens

class BrokerRateLimiter:
    """
    Manages broker API rate limits
    Enforces limits across different endpoint categories
    """
    
    # Rate limit configurations
    LIMITS = {
        'general': RateLimitConfig(
            name='General Endpoints',
            tokens_per_interval=10,
            interval_seconds=1.0,
            max_tokens=10
        ),
        'historical_candles': RateLimitConfig(
            name='Historical Candles',
            tokens_per_interval=3,
            interval_seconds=1.0,
            max_tokens=3
        ),
        'order_placement': RateLimitConfig(
            name='Order Placement',
            tokens_per_interval=400,
            interval_seconds=60.0,
            max_tokens=400
        ),
        'quote': RateLimitConfig(
            name='Quote / LTP',
            tokens_per_interval=10,
            interval_seconds=1.0,
            max_tokens=10  # Can batch up to 500 instruments per call
        )
    }
    
    def __init__(self):
        self.buckets: Dict[str, TokenBucket] = {}
        self.order_mod_count: Dict[str, int] = {}  # symbol -> mod count
        self.last_mod_reset: Dict[str, float] = {}  # symbol -> reset time
        self.mod_reset_interval = 60.0  # Reset per-order mod count every 60s
        
        # Initialize all buckets
        for bucket_name, config in self.LIMITS.items():
            self.buckets[bucket_name] = TokenBucket(config)
        
        logger.info("BrokerRateLimiter initialized with all bucket limits")
    
    def can_make_request(self, endpoint_type: str = 'general') -> bool:
        """
        Check if a request can be made immediately
        """
        bucket = self.buckets.get(endpoint_type, self.buckets['general'])
        return bucket.try_consume()
    
    async def acquire(self, endpoint_type: str = 'general', max_wait: float = 5.0) -> bool:
        """
        Acquire rate limit token, waiting if necessary
        """
        bucket = self.buckets.get(endpoint_type, self.buckets['general'])
        success = await bucket.consume_or_wait(max_wait=max_wait)
        
        if not success:
            logger.warning(f"Rate limit exceeded for {endpoint_type}, max_wait={max_wait}s exceeded")
        return success
    
    def can_modify_order(self, symbol: str) -> bool:
        """
        Check if we can modify an order for this symbol
        Max 25 modifications per order (hard limit)
        """
        now = time.time()
        
        # Reset counter if interval expired
        if symbol in self.last_mod_reset:
            if (now - self.last_mod_reset[symbol]) > self.mod_reset_interval:
                self.order_mod_count[symbol] = 0
                self.last_mod_reset[symbol] = now
        else:
            self.last_mod_reset[symbol] = now
        
        count = self.order_mod_count.get(symbol, 0)
        if count >= 25:
            logger.warning(f"Order modification limit (25) reached for {symbol}")
            return False
        
        return True
    
    def record_order_modification(self, symbol: str):
        """Record that an order was modified"""
        self.order_mod_count[symbol] = self.order_mod_count.get(symbol, 0) + 1
        logger.debug(f"Order modification recorded for {symbol}: {self.order_mod_count[symbol]}/25")
    
    def get_status(self) -> Dict[str, Dict]:
        """Get current status of all rate limit buckets"""
        status = {}
        for name, bucket in self.buckets.items():
            status[name] = {
                'name': bucket.config.name,
                'available_tokens': bucket.get_available_tokens(),
                'max_tokens': bucket.config.max_tokens,
                'tokens_per_interval': bucket.config.tokens_per_interval,
                'interval_seconds': bucket.config.interval_seconds
            }
        return status

# Global singleton instance
_rate_limiter: Optional[BrokerRateLimiter] = None

def get_rate_limiter() -> BrokerRateLimiter:
    """Get or create the global rate limiter"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = BrokerRateLimiter()
    return _rate_limiter
