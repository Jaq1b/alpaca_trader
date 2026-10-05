"""Fixed-width terminal layouts for status and performance."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionView:
    symbol: str
    side: str
    qty: float
    entry: float
    last: float
    pnl: float
    pnl_pct: float
    stop: float | None = None
    asset_class: str = "stock"


def signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def money(value: float) -> str:
    return f"${value:,.2f}"


def qty_text(qty: float, asset_class: str) -> str:
    if asset_class == "crypto":
        return f"{abs(qty):.6g}"
    rounded = round(abs(qty))
    if abs(abs(qty) - rounded) < 1e-6:
        return str(int(rounded))
    return f"{abs(qty):.2f}"


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def format_positions(rows: list[PositionView], *, show_stop: bool = True) -> str:
    if not rows:
        return "  No open positions."

    include_stop = show_stop and any(row.stop is not None for row in rows)
    if include_stop:
        header = (
            f"  {'Symbol':<10} {'Side':<6} {'Qty':>10} "
            f"{'Entry':>10} {'Last':>10} {'P&L':>12} {'Stop':>10}"
        )
    else:
        header = (
            f"  {'Symbol':<10} {'Side':<6} {'Qty':>10} "
            f"{'Entry':>10} {'Last':>10} {'P&L':>12}"
        )
    lines = [header]
    ordered = sorted(rows, key=lambda row: (row.asset_class != "crypto", row.symbol))
    for row in ordered:
        line = (
            f"  {row.symbol:<10} {row.side:<6} {qty_text(row.qty, row.asset_class):>10} "
            f"{row.entry:>10,.2f} {row.last:>10,.2f} {signed_money(row.pnl):>12}"
        )
        if include_stop:
            stop = f"{row.stop:,.2f}" if row.stop else "—"
            line += f" {stop:>10}"
        lines.append(line)
    return "\n".join(lines)


def format_account(
    *,
    mode: str,
    equity: float,
    cash: float,
    status: str,
    session: str,
    open_count: int,
) -> str:
    label = "Paper" if mode == "paper" else "Live"
    return "\n".join(
        [
            f"{label} account",
            f"  Equity          {money(equity)}",
            f"  Cash            {money(cash)}",
            f"  Status          {status}",
            f"  Session         {session}",
            f"  Open positions  {open_count}",
        ]
    )


def format_recent(rows: list[tuple[str, str, str, float, str]]) -> str:
    """rows: (when, symbol, side, pnl, reason)."""
    if not rows:
        return "  No closed trades yet."
    lines = [f"  {'When':<16} {'Symbol':<10} {'Side':<6} {'P&L':>12}  Reason"]
    for when, symbol, side, pnl, reason in rows:
        lines.append(
            f"  {when:<16} {symbol:<10} {side:<6} {signed_money(pnl):>12}  "
            f"{_clip(reason, 42)}"
        )
    return "\n".join(lines)
