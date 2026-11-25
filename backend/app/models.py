# backend/app/models.py
from datetime import datetime
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, EmailStr, Field

class User(BaseModel):
    """User model"""
    user_id: str
    email: Optional[EmailStr] = None
    name: Optional[str] = None
    broker_user_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True


class Holding(BaseModel):
    """Portfolio holding model"""
    symbol: str
    qty: int
    avg_price: float
    ltp: float
    close_price: Optional[float] = 0.0
    pnl: Optional[float] = 0.0
    day_change: Optional[float] = 0.0
    day_change_percentage: Optional[float] = 0.0
    sector: str
    exchange: Optional[str] = "NSE"
    isin: Optional[str] = None
    product: Optional[str] = "CNC"

class PortfolioAnalysis(BaseModel):
    """Portfolio analysis result"""
    total_cost: float
    total_value: float
    pnl: float
    pnl_percentage: float
    day_pnl: float = 0.0
    day_pnl_percentage: float = 0.0
    allocation: dict
    holdings_count: int
    top_gainers: List[dict] = []
    top_losers: List[dict] = []
    risk_score: float = 0.0
    risk_rating: str = "Low"

class AuthResponse(BaseModel):
    """Authentication response"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    user_id: str


class StreamTokenResponse(BaseModel):
    """Short-lived stream token response for SSE/WebSocket clients."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 300

class ErrorResponse(BaseModel):
    """Error response model"""
    error: str
    details: dict = {}
    status_code: int

