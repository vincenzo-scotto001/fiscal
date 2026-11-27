"""Synthetic market data for the offline analysis tests: a price history and a Black-Scholes
priced option chain in Cboe's delayed-quotes format. No network, same seed every run."""
from __future__ import annotations

import datetime as dt
import math
import random


def trading_days(end: dt.date, n: int) -> list[dt.date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    return days[::-1]


def price_rows(end: dt.date, n: int = 1300, start: float = 3900.0, drift: float = 0.0004, vol: float = 0.011,
               volume: float = 0.0, seed: int = 7) -> list[dict]:
    """Daily OHLCV rows like Cboe's historical endpoint: [{date, open, high, low, close, volume}]."""
    rng = random.Random(seed)
    rows, close = [], start
    for i, d in enumerate(trading_days(end, n)):
        # a pullback in the middle of the last 6 months, then a recovery, so swings and lines exist
        shock = -0.006 if n - 110 < i < n - 80 else 0.0
        open_ = close * (1 + rng.gauss(0, vol / 4))
        close = max(1.0, open_ * (1 + drift + shock + rng.gauss(0, vol)))
        high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
        rows.append({"date": d.isoformat(), "open": round(open_, 2), "high": round(high, 2), "low": round(low, 2),
                     "close": round(close, 2), "volume": int(volume * (1 + rng.random())) if volume else 0})
    return rows


def _n(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def third_friday(y: int, m: int) -> dt.date:
    d = dt.date(y, m, 15)
    while d.weekday() != 4:
        d += dt.timedelta(days=1)
    return d


def chain(spot: float, today: dt.date, root: str = "SPXW", monthly_root: str = "SPX", step: float = 25.0,
          width: float = 0.2, seed: int = 11) -> dict:
    """{timestamp, data: {current_price, options: [...]}} with calls stacked above spot and puts below."""
    rng = random.Random(seed)
    exps = set()
    d = today
    while len(exps) < 12:  # dailies/weeklies for about two and a half weeks
        if d.weekday() < 5 and d >= today:
            exps.add(d)
        d += dt.timedelta(days=1)
    y, m = today.year, today.month
    for _ in range(13):
        exps.add(third_friday(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    exps = sorted(e for e in exps if e >= today)
    monthlies = {third_friday(e.year, e.month) for e in exps}
    lo, hi = spot * (1 - width), spot * (1 + width)
    strikes = [k for k in [step * i for i in range(int(lo / step), int(hi / step) + 1)]]
    options = []
    for e in exps:
        T = max((e - today).days, 0.5) / 365
        weight = 3.0 if e in monthlies else 1.0
        for k in strikes:
            iv = 0.14 + 0.25 * max(0.0, (spot - k) / spot) + 0.05 * max(0.0, (k - spot) / spot)
            sq = iv * math.sqrt(T)
            d1 = (math.log(spot / k) + (0.04 + iv * iv / 2) * T) / sq
            d2 = d1 - sq
            gamma = _phi(d1) / (spot * sq)
            round_bonus = 4.0 if k % 100 == 0 else 1.5 if k % 50 == 0 else 1.0
            for kind in "CP":
                if kind == "C":
                    price = spot * _n(d1) - k * math.exp(-0.04 * T) * _n(d2)
                    delta = _n(d1)
                    oi_base = 3000 if k >= spot else 600
                else:
                    price = k * math.exp(-0.04 * T) * _n(-d2) - spot * _n(-d1)
                    delta = _n(d1) - 1
                    oi_base = 3500 if k <= spot else 500
                oi = int(oi_base * weight * round_bonus * (0.5 + rng.random()) * math.exp(-abs(k - spot) / (spot * 0.06)))
                r = monthly_root if (e in monthlies and monthly_root) else root
                options.append({
                    "option": f"{r}{e:%y%m%d}{kind}{int(round(k * 1000)):08d}",
                    "bid": round(max(0.0, price * 0.98), 2), "ask": round(price * 1.02 + 0.05, 2),
                    "iv": round(iv, 4), "open_interest": float(oi), "volume": float(int(oi * rng.random() * 0.3)),
                    "delta": round(delta, 4), "gamma": round(gamma, 6), "theo": round(price, 4),
                    "last_trade_price": round(price, 2),
                })
    return {"timestamp": f"{today} 10:06:11", "symbol": root,
            "data": {"symbol": f"^{monthly_root or root}", "current_price": spot, "close": spot, "options": options}}
