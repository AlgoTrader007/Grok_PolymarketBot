# polybtc — Polymarket 5-minute "Bitcoin Up or Down", PAPER ONLY

Four roles, one small asyncio process (+ a morning report script):

| Role | Where | What it does |
|---|---|---|
| **Scout** | `bot.py` (rtds_loop, update_sigma) + `model.py` | Streams Chainlink BTC/USD **spot and 60s TWAP** (the settlement stream) from Polymarket's public RTDS socket, records each window's price-to-beat (TWAP60 at the start second), estimates vol, computes fair P(Up)/P(Down). |
| **Trader** | `bot.py` (trader_eval) | Each side: `edge = fair − (best_ask + fee_per_share)`. Flags a **paper** entry when edge ≥ 0.05 and ask ≤ 0.70, $15 notional, walking real book depth (only levels that still meet both rules). Max 1 entry per side per window. |
| **Settler** | `bot.py` (settle_loop) | Provisional result from TWAP60 at the end second, then final result from Polymarket's own Gamma record (`outcomePrices`/`eventMetadata.finalPrice`); logs any mismatch. PnL includes fees. |
| **Risk** | `risk_report.py` | Daily review + green-week PASS/FAIL. |
| **Builder** | this repo | `run.sh`, `polybtc.service`, `view.py`. |

**Real trading is impossible in this code:** there is no order-placing code, no signing, no wallet, no API keys. Only public GET/WebSocket reads. Switching to real bets would need new code, a Polymarket account in an eligible jurisdiction, and the user's explicit approval.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
setsid nohup ./run.sh >/dev/null 2>&1 &     # restart-on-crash wrapper
.venv/bin/python view.py                     # live terminal (Ctrl-C to quit; bot keeps running)
.venv/bin/python risk_report.py --day 2026-11-03
./stop.sh                                    # stop wrapper + bot
```
On a $5 VPS use systemd instead: copy to `/opt/polybtc`, edit `polybtc.service`, `systemctl enable --now polybtc`.
Uses ~40 MB RAM, ~1 request/2 s to RTDS, a Gamma call per window, REST book resync every 15 s.

## Config
All knobs in `config.toml` (edge threshold, max price, size, vol window, staleness guards).
The fee rate is read per market from Gamma `feeSchedule` (currently `rate 0.07, exponent 1`, taker-only):
`fee_per_share = 0.07 × p × (1−p)` → 1.75¢/share at 50¢, 1.47¢ at 70¢.

## Model (Scout)
Settlement: Up iff TWAP60(T_end) ≥ TWAP60(T_start). Spot = driftless GBM, per-second vol σ.
* ≥60 s left: `P(Up) = Φ( ln(S/K) / (σ·√(τ−40)) )` — the brief's `Φ(ln(S/S0)/(σ√t))` with effective time τ−40 because the final price is a 60-s average (variance of an average over 60 s = 60/3).
* <60 s left: anchor on the live official TWAP: `E[TWAP_end] = TWAP(t) + (τ·S − Σ S_{u−60})/60`, `sd = S·σ·√(τ³/3)/60`.
* σ: 5-second log returns of Chainlink spot, winsorized at 6×MAD, blend of 30-min and 5-min realized vol, seeded from Coinbase 1-min candles at startup until 30 min of history exists, floored 15%/capped 300% annualized.
* Feed lag (~1.3–3 s) is handled by computing τ from the data timestamp, and no entries in the final 10 s.

## Logs (`logs/`, timestamps America/Los_Angeles)
* `signals.jsonl` – every edge ≥ threshold (incl. blocked ones and why)
* `trades.jsonl` – paper entries and settlements; `trades.csv` – one row per settled trade
* `ticks.jsonl` – 5-s model/market snapshots (for calibration); `events.jsonl` – discovery, price-to-beat, settlements, anomalies, restarts
* `state.json` – live state for `view.py`; `bot.out` – stderr

## Data sources
* Gamma: `https://gamma-api.polymarket.com/events?slug=btc-updown-5m-<unix start, multiple of 300>`
* Books: `wss://ws-subscriptions-clob.polymarket.com/ws/market`, `https://clob.polymarket.com/book?token_id=`
* Chainlink: legacy RTDS `wss://ws-live-data.polymarket.com`, topics `crypto_prices_chainlink` + `crypto_prices_twap_sixty` (`btc/usd`). It now returns a 60-s snapshot per subscribe but no live push, so we re-subscribe every 2 s. Polymarket's replacement (PolyBolt `wss://ws-live-v2.polymarket.com/ws`, `price.crypto.twap`) needs CLOB API credentials. If the legacy socket is shut off, the bot stops signalling (feed_stale) and logs `feed_gap`.

## Notes from the first live run (2026-11-03 PT)
* Without the Coinbase lag nowcast, the order book ran 3–5 s ahead of the Chainlink snapshot, and the first "edges" were just stale data (e.g. a 20-point "edge" on Down at 21:40:11 that lost). With the nowcast, Scout's P(Up) tracks the market within ~1–3 points. Expect most 5-point edges to be adverse selection until a week of paper results says otherwise.
* Our price-to-beat (TWAP60 at the start second) matched Polymarket's official `priceToBeat` exactly. Gamma publishes `priceToBeat` only after the window ends, and the official result 3–10 minutes later.
* The two trades on 1793770800 (21:40 PT) came from v1, before the nowcast was added.
