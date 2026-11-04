"""Scout model: fair P(Up) for a Chainlink TWAP60-settled 5-minute window.

Settlement (since 2026-08-14): Up iff TWAP60(T_end) >= TWAP60(T_start) = K.
TWAP60(x) = average of Chainlink BTC/USD spot over [x-60, x].

Spot S follows driftless GBM with per-second vol sigma.
* tau = T_end - t >= 60: the whole final averaging window is in the future.
  Var[ln TWAP_end] ~= sigma^2 * ((tau-60) + 60/3) = sigma^2 * (tau-40)
  => P(Up) = Phi( ln(S/K) / (sigma*sqrt(tau-40)) )
  i.e. the brief's Phi(ln(S/S0)/(sigma*sqrt(t))) with an effective time tau-40
  that accounts for the averaging.
* 0 < tau < 60: part of the final window is already observed. Anchor on the
  latest official TWAP60(t):
    TWAP_end = TWAP(t) + (1/60) * sum_{u in (t, T_end]} (S_u - S_{u-60})
  S_{u-60} are already-observed spots (B); future S_u average to S_t with
  sd = S*sigma*sqrt(tau^3/3)/60.
    => mean = TWAP(t) + (tau*S - B)/60
"""
import math

SECS_PER_YEAR = 365 * 24 * 3600


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket taker fee per share: rate * (p*(1-p))^exponent (docs.polymarket.com/trading/fees)."""
    return rate * (price * (1.0 - price)) ** exponent


def realized_sigma(spot_by_sec: dict, now_s: int, window_s: int, step_s: int):
    """Per-second vol from step_s log returns over [now-window, now], forward-filled.
    Returns are winsorized at 6*MAD to resist bad ticks. None if too little data."""
    start = now_s - window_s
    secs = [s for s in range(start, now_s + 1, step_s)]
    vals, last = [], None
    keys_lo = min(spot_by_sec) if spot_by_sec else now_s
    for s in secs:
        v = spot_by_sec.get(s)
        if v is None:
            # forward fill from up to step_s-1 seconds earlier
            for k in range(1, step_s):
                v = spot_by_sec.get(s - k)
                if v is not None:
                    break
        if v is None and s < keys_lo:
            continue
        if v is None:
            v = last
        if v is not None:
            vals.append(v)
            last = v
    rets = [math.log(b / a) for a, b in zip(vals, vals[1:]) if a > 0 and b > 0]
    if len(rets) < 12:
        return None, len(rets)
    med = sorted(rets)[len(rets) // 2]
    mad = sorted(abs(r - med) for r in rets)[len(rets) // 2] or 1e-9
    lim = 6 * 1.4826 * mad
    w = [max(-lim, min(lim, r)) for r in rets]
    var = sum(r * r for r in w) / len(w)
    return math.sqrt(var / step_s), len(rets)


def ann_to_sec(a: float) -> float:
    return a / math.sqrt(SECS_PER_YEAR)


def sec_to_ann(s: float) -> float:
    return s * math.sqrt(SECS_PER_YEAR)


def fair_p_up(K, S, tau, sigma, twap_now=None, spot_by_sec=None, t_now=None, clip=0.002):
    """Return (p_up, method)."""
    if K is None or S is None or sigma is None:
        return None, "missing"
    if tau <= 0:
        return (1.0 if (twap_now or S) >= K else 0.0), "expired"
    if tau >= 60 or twap_now is None or spot_by_sec is None:
        t_eff = max(tau - 40.0, 1e-6) if tau >= 60 else max(tau, 1e-6)
        z = math.log(S / K) / (sigma * math.sqrt(t_eff))
        p, m = phi(z), "gbm_twap"
    else:
        T_end = t_now + tau
        B, last = 0.0, S
        # spots at u-60 for u in (t, T_end]  ==> seconds (t-60, T_end-60]
        for sec in range(int(t_now) - 59, int(T_end) - 59):
            v = spot_by_sec.get(sec)
            if v is None:
                v = spot_by_sec.get(sec - 1, last)
            B += v
            last = v
        mean = twap_now + (tau * S - B) / 60.0
        sd = S * sigma * math.sqrt(tau ** 3 / 3.0) / 60.0
        sd = math.sqrt(sd * sd + (1e-6 * K) ** 2)  # small model/rounding floor
        p, m = phi((mean - K) / sd), "twap_anchor"
    return min(1 - clip, max(clip, p)), m
