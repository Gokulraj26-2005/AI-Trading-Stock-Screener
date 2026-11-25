def nse_round(value: float) -> float:
    """Round value to the nearest NSE tick size (0.05)."""
    return round(value / 0.05) * 0.05

from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

def compute_net_r(
    entry_price: float,
    exit_price: float,
    quantity: int,
    direction: str,
    stop_price: float,
    gross_r: float
) -> Dict[str, Any]:
    """
    Computes total round-trip transaction costs for one intraday NSE equity MIS trade
    and deduces it from gross R.
    """
    notional_buy = entry_price * quantity if direction == "BUY" else exit_price * quantity
    notional_sell = exit_price * quantity if direction == "BUY" else entry_price * quantity

    # 0.025% on sell side only for intraday equity
    stt = notional_sell * 0.00025

    # NSE transaction charge both sides
    exchange_charges = (notional_buy + notional_sell) * 0.0000345

    # 0.003% on buy side only
    stamp_duty = notional_buy * 0.00003

    # SEBI turnover fee both sides
    sebi_fee = (notional_buy + notional_sell) * 0.000001

    # Standard broker flat fee: ₹20 per leg × 2 legs = ₹40
    brokerage = 40.00

    # 18% GST on brokerage and exchange charges only
    gst_on_brokerage = (brokerage + exchange_charges) * 0.18

    total_cost_rs = stt + exchange_charges + stamp_duty + sebi_fee + brokerage + gst_on_brokerage

    risk_rs = abs(entry_price - stop_price) * quantity

    if risk_rs <= 0:
        logger.warning(f"compute_net_r: risk_rs is zero (entry={entry_price}, stop={stop_price}). Returning gross_r unchanged.")
        return {
            "gross_r": gross_r,
            "net_r": gross_r,
            "total_cost_rs": round(total_cost_rs, 2),
            "cost_in_r": 0.0,
            "cost_breakdown": {
                "stt": round(stt, 2),
                "exchange_charges": round(exchange_charges, 2),
                "stamp_duty": round(stamp_duty, 2),
                "sebi_fee": round(sebi_fee, 2),
                "brokerage": round(brokerage, 2),
                "gst": round(gst_on_brokerage, 2)
            }
        }

    cost_in_r = total_cost_rs / risk_rs
    net_r = gross_r - cost_in_r

    return {
        "gross_r": gross_r,
        "net_r": net_r,
        "total_cost_rs": round(total_cost_rs, 2),
        "cost_in_r": round(cost_in_r, 4),
        "cost_breakdown": {
            "stt": round(stt, 2),
            "exchange_charges": round(exchange_charges, 2),
            "stamp_duty": round(stamp_duty, 2),
            "sebi_fee": round(sebi_fee, 2),
            "brokerage": round(brokerage, 2),
            "gst": round(gst_on_brokerage, 2)
        }
    }
