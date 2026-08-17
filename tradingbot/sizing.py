"""Shared position sizing: equity risk × conviction, capped by max notional."""


def conviction_mult(score: int, score_min: int) -> float:
    """Scale size with signal strength: at floor = 1.0, +0.15 per point, cap 1.3."""
    extra = max(0, int(score) - int(score_min))
    return min(1.3, 1.0 + 0.15 * extra)


def quantity_for_risk(
    *,
    equity: float,
    risk_per_trade: float,
    entry_price: float,
    stop_loss: float,
    asset_class: str,
    score: int,
    score_min: int,
    max_position_value: float,
    min_position_value: float,
) -> float:
    """
    Shares/coins to buy (or short) so stop distance ≈ equity × risk × conviction.
    Returns 0 when the trade cannot clear min_position_value under the max cap.
    """
    if entry_price <= 0 or equity <= 0 or risk_per_trade <= 0:
        return 0.0

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        return 0.0

    risk_amount = equity * risk_per_trade * conviction_mult(score, score_min)
    qty = risk_amount / stop_distance

    if asset_class == "crypto":
        qty = min(qty, max_position_value / entry_price)
        if qty * entry_price < min_position_value:
            return 0.0
        return float(qty)

    max_shares = int(max_position_value / entry_price)
    if max_shares < 1:
        return 0.0
    qty = min(max(1, int(qty)), max_shares)
    if qty * entry_price < min_position_value:
        return 0.0
    return float(qty)


def unrealized_pnl(
    entry_price: float, current_price: float, quantity: float, side: str = "long"
) -> tuple[float, float]:
    """Return (pnl_dollars, pnl_pct) for a long or short."""
    qty = abs(quantity)
    if entry_price <= 0 or qty <= 0:
        return 0.0, 0.0
    if side == "short":
        pnl = (entry_price - current_price) * qty
        pct = (entry_price - current_price) / entry_price * 100
    else:
        pnl = (current_price - entry_price) * qty
        pct = (current_price - entry_price) / entry_price * 100
    return pnl, pct
