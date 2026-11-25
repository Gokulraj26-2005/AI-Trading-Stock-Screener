# backend/app/logger.py
import logging
import sys
import contextvars
from datetime import datetime
from pathlib import Path

# Create logs directory
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Configure logging format
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - correlation_id=%(correlation_id)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def setup_logger(name: str = "ai_portfolio", level: int = logging.INFO):
    """
    Set up structured logging for the application.
    
    Args:
        name: Logger name
        level: Logging level (default: INFO)
    
    Returns:
        Configured logger instance
    """
    # Configure root logger to capture all module logs
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Configure specific logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if not root_logger.handlers:
        # Reconfigure sys.stdout encoding for UTF-8 compatibility
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)
        
        # File handler - rotating daily
        log_file = LOG_DIR / f"app_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
        
    return logger

# Create default logger
logger = setup_logger()

_correlation_id = contextvars.ContextVar("correlation_id", default=None)


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get() or "-"
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(CorrelationFilter())


def set_correlation_id(value: str) -> contextvars.Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: contextvars.Token) -> None:
    _correlation_id.reset(token)

def log_request(method: str, path: str, status_code: int, duration_ms: float):
    """Log HTTP request details"""
    logger.info(f"{method} {path} - {status_code} - {duration_ms:.2f}ms")

def log_error(error: Exception, context: dict = None):
    """Log error with context"""
    logger.error(f"Error: {str(error)}", extra={"context": context or {}}, exc_info=True)

def log_broker_action(action: str, user_id: str, details: dict = None):
    """Log Broker actions"""
    logger.info(f"Broker Action: {action} - User: {user_id}", extra={"details": details or {}})

def log_llm_request(model_id: str, message_length: int, response_length: int = None):
    """Log LLM requests"""
    logger.info(f"LLM Request: {model_id} - Input: {message_length} chars - Output: {response_length or 'streaming'} chars")
