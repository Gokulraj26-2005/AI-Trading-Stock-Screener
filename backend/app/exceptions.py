# backend/app/exceptions.py
from typing import Optional, Dict, Any

class AIPortfolioException(Exception):
    """Base exception for AI Portfolio application"""
    def __init__(self, message: str, status_code: int = 500, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

class AuthenticationError(AIPortfolioException):
    """Raised when authentication fails"""
    def __init__(self, message: str = "Authentication failed", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=401, details=details)

class AuthorizationError(AIPortfolioException):
    """Raised when user is not authorized"""
    def __init__(self, message: str = "Not authorized", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=403, details=details)

class BrokerAPIError(AIPortfolioException):
    """Raised when broker API fails"""
    def __init__(self, message: str = "Broker API error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=502, details=details)

class LLMError(AIPortfolioException):
    """Raised when LLM service fails"""
    def __init__(self, message: str = "LLM service error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=502, details=details)

class ValidationError(AIPortfolioException):
    """Raised when input validation fails"""
    def __init__(self, message: str = "Validation error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=400, details=details)

class RateLimitError(AIPortfolioException):
    """Raised when rate limit is exceeded"""
    def __init__(self, message: str = "Rate limit exceeded", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=429, details=details)

class DatabaseError(AIPortfolioException):
    """Raised when database operation fails"""
    def __init__(self, message: str = "Database error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=500, details=details)

class NotFoundError(AIPortfolioException):
    """Raised when resource is not found"""
    def __init__(self, message: str = "Resource not found", details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=404, details=details)
