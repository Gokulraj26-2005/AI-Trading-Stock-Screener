import zmq
import zmq.asyncio
import msgpack
import logging
import asyncio
from typing import Dict, Any, Union

# Pre-encoded topic cache to eliminate per-tick string encoding allocations
_TOPIC_BYTES_CACHE: Dict[str, bytes] = {}

def get_topic_bytes(topic: str) -> bytes:
    b = _TOPIC_BYTES_CACHE.get(topic)
    if b is None:
        b = topic.encode("utf-8")
        if len(_TOPIC_BYTES_CACHE) < 2048:
            _TOPIC_BYTES_CACHE[topic] = b
    return b

logger = logging.getLogger(__name__)

class ZMQPublisher:
    """
    ZeroMQ Message Bus Publisher.
    Serializes enriched ticks via MessagePack and publishes immediately.
    """
    def __init__(self, port: int = 5555):
        self.port = port
        self.context = zmq.asyncio.Context()
        self.socket = self.context.socket(zmq.PUB)
        self.socket.setsockopt(zmq.SNDHWM, 10000)
        self.socket.setsockopt(zmq.TCP_KEEPALIVE, 1)
        self.socket.bind(f"tcp://127.0.0.1:{self.port}")
        self.running = True  # Auto-start: ticks must never be silently dropped
        logger.info(f"ZMQPublisher initialized on tcp://127.0.0.1:{self.port}")

    async def start(self) -> None:
        """Start the publisher service."""
        self.running = True
        logger.info("ZMQPublisher started")

    async def stop(self) -> None:
        """Stop the publisher service."""
        self.running = False
        self.socket.close()
        self.context.term()
        logger.info("ZMQPublisher stopped")

    async def publish_tick(self, tick_data: Dict[str, Any]) -> None:
        """
        Publish tick data on the ZMQ PUB socket.
        """
        sym = tick_data.get('symbol')
        topic = f"TICK_{sym}" if sym else "TICK_TICKS"
        await self.publish(topic, tick_data)

    async def publish(self, topic: Union[str, bytes], payload_data: Dict[str, Any]) -> None:
        """Publish an arbitrary dictionary payload to a ZMQ topic."""
        if not self.running:
            return

        try:
            payload = msgpack.packb(payload_data)
            topic_bytes = topic if isinstance(topic, bytes) else get_topic_bytes(topic)
            await self.socket.send_multipart([topic_bytes, payload])
        except Exception as e:
            logger.error(f"Error publishing ZMQ message on {topic}: {e}")

    async def health_check(self) -> bool:
        """
        Check if publisher is healthy.
        
        Returns True if:
        - Publisher is running
        - Socket is not closed
        - Context is not terminated
        """
        try:
            if not self.running:
                return False
            if self.socket.closed:
                return False
            if self.context.closed:
                return False
            return True
        except Exception:
            return False

    async def get_status(self) -> Dict[str, Any]:
        """Get publisher status with detailed health information."""
        return {
            'status': 'running' if self.running else 'stopped',
            'port': self.port,
            'socket_closed': self.socket.closed,
            'context_closed': self.context.closed if hasattr(self.context, 'closed') else False,
            'healthy': await self.health_check()
        }

    async def publish_health_check(self) -> None:
        """
        Publish a health check message on the HEALTH_CHECK topic.
        Subscribers can use this to verify bidirectional communication.
        """
        import time
        health_payload = {
            'timestamp': time.time(),
            'status': 'healthy' if await self.health_check() else 'unhealthy',
            'port': self.port
        }
        await self.publish("HEALTH_CHECK", health_payload)

# Global instance for use across async event loop
_zmq_publisher = None

def get_zmq_publisher():
    global _zmq_publisher
    if _zmq_publisher is None or not getattr(_zmq_publisher, 'running', False) or _zmq_publisher.socket.closed:
        _zmq_publisher = ZMQPublisher(port=5555)
    return _zmq_publisher
