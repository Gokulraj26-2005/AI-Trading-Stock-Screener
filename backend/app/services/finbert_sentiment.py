"""
FinBERT Sentiment & Narrative Intelligence Service for VIA-Agent7
==================================================================
Runs the 110M-parameter ProsusAI/finbert model with CUDA FP16 hardware
acceleration on host GPU (with automatic fallback to CPU).

Provides:
- Real-time batched headline and regulatory filing sentiment scoring
- Probability breakdown: Positive, Neutral, Negative
- Normalized Net Sentiment Score (-100 to +100)
- 15-minute thread-safe in-memory LRU caching
- Cross-modal Narrative Alignment synthesis against Institutional Flow Profile (IFP)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Model identifier
FINBERT_MODEL_NAME = "ProsusAI/finbert"

# In-memory cache: symbol -> (timestamp, result_dict)
_SENTIMENT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SENTIMENT_CACHE_TTL = 900  # 15 minutes
_CACHE_LOCK = threading.Lock()


class FinBERTSentimentEngine:
    """
    Thread-safe GPU-accelerated financial sentiment analysis engine.
    """

    _instance: Optional["FinBERTSentimentEngine"] = None
    _init_lock = threading.Lock()

    def __init__(self):
        self._model = None
        self._tokenizer = None
        self._device = "cpu"
        self._is_ready = False
        self._load_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "FinBERTSentimentEngine":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_loaded(self) -> None:
        """Lazy load FinBERT tokenizer and model onto GPU or CPU."""
        if self._is_ready:
            return

        with self._load_lock:
            if self._is_ready:
                return

            try:
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer

                device = "cuda" if torch.cuda.is_available() else "cpu"
                logger.info(f"Initializing FinBERT Sentiment Engine on device: {device}...")

                tokenizer = AutoTokenizer.from_pretrained(FINBERT_MODEL_NAME)
                model = AutoModelForSequenceClassification.from_pretrained(FINBERT_MODEL_NAME)

                if device == "cuda":
                    model = model.to(device=device, dtype=torch.float16)
                else:
                    model = model.to(device=device, dtype=torch.float32)

                model.eval()

                # Warm-up pass to compile CUDA kernels
                with torch.no_grad():
                    warmup_inputs = tokenizer(
                        ["Market update"], return_tensors="pt", padding=True, truncation=True
                    ).to(device)
                    _ = model(**warmup_inputs)

                self._tokenizer = tokenizer
                self._model = model
                self._device = device
                self._is_ready = True
                logger.info(f"FinBERT Sentiment Engine successfully loaded on {device} (FP16={device == 'cuda'}).")
            except Exception as exc:
                logger.error(f"Failed to load FinBERT model: {exc}. Falling back to rule-based neutral.", exc_info=True)
                self._is_ready = False

    def get_cached_analysis(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retrieve unexpired cached sentiment analysis if present."""
        now = time.time()
        with _CACHE_LOCK:
            if symbol in _SENTIMENT_CACHE:
                cached_ts, data = _SENTIMENT_CACHE[symbol]
                if now - cached_ts < _SENTIMENT_CACHE_TTL:
                    return data
        return None

    def cache_analysis(self, symbol: str, data: Dict[str, Any]) -> None:
        """Store sentiment analysis in thread-safe LRU cache."""
        now = time.time()
        with _CACHE_LOCK:
            _SENTIMENT_CACHE[symbol] = (now, data)

    def analyze_articles(
        self,
        symbol: str,
        articles: List[Dict[str, Any]],
        institutional_flow_score: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Analyzes a list of news headlines/filings for a given stock symbol.

        Each article dict expects:
        - title: str
        - source: str (optional)
        - hours_ago: int (optional)
        - is_regulatory: bool (optional, default False)
        """
        # 1. Check cache first
        cached = self.get_cached_analysis(symbol)
        if cached is not None:
            # Refresh narrative alignment if fresh institutional flow score is provided
            if institutional_flow_score is not None and cached.get("sentiment_score") is not None:
                cached["narrative_alignment"] = self._compute_narrative_alignment(
                    cached["sentiment_score"], institutional_flow_score
                )
            return cached

        # 2. Fast path for no news
        if not articles:
            now_iso = datetime.now(timezone.utc).isoformat()
            result = {
                "sentiment_score": 0,
                "sentiment_label": "NEUTRAL",
                "probabilities": {"positive": 0.33, "neutral": 0.34, "negative": 0.33},
                "article_count": 0,
                "headlines": [],
                "narrative_alignment": self._compute_narrative_alignment(0, institutional_flow_score),
                "summary": "Quiet news cycle in the past 24h. No significant corporate catalysts detected.",
                "scanned_at": now_iso,
                "lookback_window": "24h",
            }
            self.cache_analysis(symbol, result)
            return result

        # 3. Ensure neural model loaded
        self._ensure_loaded()

        if not self._is_ready or self._model is None or self._tokenizer is None:
            # Fallback heuristic if GPU/transformers failed to initialize
            result = self._fallback_rule_analysis(symbol, articles, institutional_flow_score)
            self.cache_analysis(symbol, result)
            return result

        # 4. Neural Batch Inference
        try:
            import torch

            texts = [a.get("title", "").strip() for a in articles if a.get("title")]
            if not texts:
                return self.analyze_articles(symbol, [], institutional_flow_score)

            inputs = self._tokenizer(
                texts, return_tensors="pt", padding=True, truncation=True, max_length=128
            ).to(self._device)

            with torch.no_grad():
                outputs = self._model(**inputs)
                # Ensure float32 for softmax stability
                probs = torch.softmax(outputs.logits.float(), dim=-1).cpu().numpy()

            # FinBERT class mapping: 0 -> positive, 1 -> negative, 2 -> neutral
            pos_col = probs[:, 0]
            neg_col = probs[:, 1]
            neu_col = probs[:, 2]

            scored_headlines: List[Dict[str, Any]] = []
            item_scores: List[float] = []
            weights: List[float] = []

            for idx, art in enumerate(articles):
                p_pos = float(pos_col[idx])
                p_neg = float(neg_col[idx])
                p_neu = float(neu_col[idx])

                # Continuous single-item score from -100 to +100
                item_score = int(round((p_pos - p_neg) * 100.0))
                item_scores.append(item_score)

                if p_pos > p_neg and p_pos > p_neu:
                    sentiment_tag = "BULLISH"
                elif p_neg > p_pos and p_neg > p_neu:
                    sentiment_tag = "BEARISH"
                else:
                    sentiment_tag = "NEUTRAL"

                # Regulatory/BSE announcements receive higher catalyst weighting (1.5x)
                w = 1.5 if art.get("is_regulatory") else 1.0
                weights.append(w)

                scored_headlines.append({
                    "title": art.get("title", ""),
                    "source": art.get("source", "News"),
                    "hours_ago": art.get("hours_ago", 1),
                    "sentiment": sentiment_tag,
                    "score": item_score,
                    "is_regulatory": bool(art.get("is_regulatory", False)),
                    "prob_positive": round(p_pos, 3),
                    "prob_negative": round(p_neg, 3),
                    "prob_neutral": round(p_neu, 3),
                })

            total_weight = sum(weights) or 1.0
            avg_score = sum(s * w for s, w in zip(item_scores, weights)) / total_weight
            final_score = int(max(-100, min(100, round(avg_score))))

            # Average probabilities across items
            avg_pos = round(float(sum(pos_col[i] * weights[i] for i in range(len(texts))) / total_weight), 3)
            avg_neg = round(float(sum(neg_col[i] * weights[i] for i in range(len(texts))) / total_weight), 3)
            avg_neu = round(float(sum(neu_col[i] * weights[i] for i in range(len(texts))) / total_weight), 3)

            # Categorize label
            if final_score >= 45:
                label = "STRONG_BULLISH"
            elif final_score >= 15:
                label = "BULLISH"
            elif final_score <= -45:
                label = "STRONG_BEARISH"
            elif final_score <= -15:
                label = "BEARISH"
            else:
                label = "NEUTRAL"

            alignment = self._compute_narrative_alignment(final_score, institutional_flow_score)

            summary = self._generate_sentiment_summary(
                symbol=symbol,
                score=final_score,
                label=label,
                headlines=scored_headlines,
                alignment=alignment,
            )

            result = {
                "sentiment_score": final_score,
                "sentiment_label": label,
                "probabilities": {
                    "positive": avg_pos,
                    "neutral": avg_neu,
                    "negative": avg_neg,
                },
                "article_count": len(scored_headlines),
                "headlines": scored_headlines,
                "narrative_alignment": alignment,
                "summary": summary,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "lookback_window": "24h",
            }

            self.cache_analysis(symbol, result)
            return result

        except Exception as exc:
            logger.error(f"FinBERT inference failed for {symbol}: {exc}", exc_info=True)
            return self._fallback_rule_analysis(symbol, articles, institutional_flow_score)

    def _compute_narrative_alignment(
        self, sentiment_score: int, ifp_score: Optional[int]
    ) -> Dict[str, Any]:
        """
        Evaluates the cross-modal interaction between external news sentiment
        and internal institutional tape order flow (IFP).
        """
        if ifp_score is None:
            return {
                "state": "UNALIGNED",
                "badge": "Flow Baseline Pending",
                "type": "neutral",
                "detail": "Institutional tape score pending for narrative cross-check.",
            }

        # 1. Concordant Bullish: Positive media confirmed by institutional accumulation
        if sentiment_score >= 25 and ifp_score >= 25:
            return {
                "state": "CONCORDANT_BULLISH",
                "badge": "Concordant Flow",
                "type": "success",
                "detail": "Catalyst Confirmation: Positive media narrative is validated by institutional accumulation on the tape.",
            }

        # 2. Concordant Bearish: Negative media confirmed by institutional distribution
        if sentiment_score <= -25 and ifp_score <= -25:
            return {
                "state": "CONCORDANT_BEARISH",
                "badge": "Concordant Headwinds",
                "type": "danger",
                "detail": "Confirmed Headwinds: Negative news sentiment aligns with institutional distribution.",
            }

        # 3. Media Hype Trap: Glowing headlines, but institutions are net distributing
        if sentiment_score >= 25 and ifp_score <= -25:
            return {
                "state": "MEDIA_HYPE_TRAP",
                "badge": "Distribution into Hype",
                "type": "warning",
                "detail": "Caution: Positive media headlines are being absorbed by institutional distribution (potential liquidity trap).",
            }

        # 4. Silent Institutional Accumulation: Quiet/negative headlines, but smart money buying
        if sentiment_score <= 10 and ifp_score >= 35:
            return {
                "state": "SILENT_ACCUMULATION",
                "badge": "Silent Institutional Build",
                "type": "info",
                "detail": "Smart Money Footprint: Institutions are aggressively accumulating despite quiet or negative media sentiment.",
            }

        return {
            "state": "NEUTRAL_BALANCED",
            "badge": "Balanced Narrative",
            "type": "neutral",
            "detail": "News narrative and tape order flow are in a neutral equilibrium.",
        }

    def _generate_sentiment_summary(
        self,
        symbol: str,
        score: int,
        label: str,
        headlines: List[Dict[str, Any]],
        alignment: Dict[str, Any],
    ) -> str:
        """Generates an executive one-paragraph narrative synthesis."""
        score_str = f"+{score}" if score > 0 else f"{score}"
        has_bse = any(h.get("is_regulatory") for h in headlines)
        bse_tag = " (including verified regulatory disclosures)" if has_bse else ""

        narrative = (
            f"FinBERT Sentiment: {score_str}/100 [{label.replace('_', ' ')}] across "
            f"{len(headlines)} headline catalyst(s){bse_tag}. "
            f"{alignment.get('detail', '')}"
        )
        return narrative

    def _fallback_rule_analysis(
        self,
        symbol: str,
        articles: List[Dict[str, Any]],
        institutional_flow_score: Optional[int],
    ) -> Dict[str, Any]:
        """Deterministic keyword fallback if GPU/Transformers is unavailable."""
        bull_words = {"profit", "surge", "gain", "deal", "growth", "dividend", "order", "win", "high", "upgrade"}
        bear_words = {"loss", "fall", "drop", "penalty", "probe", "fraud", "downgrade", "debt", "crash", "low"}

        scored_headlines = []
        scores = []
        for art in articles:
            t_lower = art.get("title", "").lower()
            b_cnt = sum(1 for w in bull_words if w in t_lower)
            r_cnt = sum(1 for w in bear_words if w in t_lower)
            diff = b_cnt - r_cnt
            s = int(max(-80, min(80, diff * 40)))
            scores.append(s)
            tag = "BULLISH" if s > 15 else ("BEARISH" if s < -15 else "NEUTRAL")
            scored_headlines.append({
                "title": art.get("title", ""),
                "source": art.get("source", "News"),
                "hours_ago": art.get("hours_ago", 1),
                "sentiment": tag,
                "score": s,
                "is_regulatory": bool(art.get("is_regulatory", False)),
                "prob_positive": 0.7 if s > 0 else 0.1,
                "prob_negative": 0.7 if s < 0 else 0.1,
                "prob_neutral": 0.6 if s == 0 else 0.2,
            })

        avg_score = int(sum(scores) / max(1, len(scores)))
        label = "BULLISH" if avg_score >= 20 else ("BEARISH" if avg_score <= -20 else "NEUTRAL")
        alignment = self._compute_narrative_alignment(avg_score, institutional_flow_score)

        return {
            "sentiment_score": avg_score,
            "sentiment_label": label,
            "probabilities": {"positive": 0.33, "neutral": 0.34, "negative": 0.33},
            "article_count": len(scored_headlines),
            "headlines": scored_headlines,
            "narrative_alignment": alignment,
            "summary": f"Keyword sentiment baseline: {avg_score}/100. {alignment['detail']}",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "lookback_window": "24h",
        }


# Global singleton accessor
def get_finbert_sentiment_engine() -> FinBERTSentimentEngine:
    return FinBERTSentimentEngine.get_instance()
