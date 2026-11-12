# polybtc — Polymarket BTC 5-minute Up/Down

Paper mode is the default. The bot watches public BTC 5-minute markets and can
place real orders only after you turn that on yourself, on the machine that
holds the keys. Nothing in this repository enables a funded account, and a
green week in the log does not turn live mode on.

Historical paper results in `logs/` are unreliable and incomplete. The earlier
paper run could buy both sides of one window, keep a partial size when the book
was thinner than $15, trade after a price-feed gap, and book a missing close as
`UNKNOWN` with a full loss. Those rows are not a track record. This change does
not alter the probability model in `model.py` and does not claim that model is
calibrated.

## What it trades

BTC 5-minute Up/Down markets only (`btc-updown-5m-<unix start>`).

| Rule | Value |
|---|---|
| Price cap | 0.70 per share |
| Minimum net edge | 0.05 per share after the market's taker fee |
| Stake | $15 of pUSD per entry, and only when the displayed book can fill all of it |
| Direction | one side per window |
| Default mode | paper |

Checked against public data on 2026-11-10 (no order was sent):

* Gamma `feeSchedule` was `rate 0.07`, `exponent 1`, taker only. That matches the published crypto fee `fee = shares × 0.07 × price × (1 − price)`, rounded to 5 decimal pUSD. At 100 shares the fee is $1.75 at 0.50 and $1.47 at 0.70. Makers pay zero. This bot takes liquidity, so the fee applies. `takerBaseFee: 1000` on that payload is not used as the rate.
* The order book reported `tick_size` 0.01 and `min_order_size` 5. CLOB market info reported `mts` 0.001 and `mos` 5. Prices must fit the coarser 0.01 tick. If those tick sources disagree in a way that is not a multiple, the bot does not send an order.
* The market version was `v1` (CTF token ids). A `v2` market is not traded.
* Resolution source: Chainlink BTC/USD 60-second TWAP (`https://data.chain.link/streams/btc-usd-twap-60s-streams`). Up if the final TWAP is greater than or equal to the TWAP at the start. Down otherwise. Coinbase prices are only a lag nowcast for the unchanged model. They are not the settlement price.

## What a live order actually does

The displayed book must already show at least $15 of asks at prices that are on the tick, at or below 0.70, and still have at least 0.05 of edge after the fee. If it does not, the bot skips the window. It does not shrink the stake to whatever size is sitting there.

When the book passes, live mode sends one Fill-And-Kill (FAK) buy through the official `polymarket-client` (`AsyncSecureClient.create_market_order` then `post_order`) for the $15 amount. `max_price` is the worst ask on that snapshot that still cleared the edge and the 0.70 cap, not a blank market buy.

Unavoidable exchange behavior, from the current order docs:

* The book can move after the check. FAK then buys only the shares still available at or below `max_price` and cancels the rest. A fill can be smaller than $15, or zero. The bot keeps the actual fill and does not send a second order to top the position up.
* A zero fill that the API confirms (`fak_not_filled`, or a reconcile that proves the order never landed) may be retried once, on the same side only. The other side stays blocked for that window.
* If the HTTP response is lost, the intent is already on disk as `submit_unknown`. The bot looks up the order and account trades before it will send anything else. If the lookup fails, or more than one trade could match, it stays unresolved and does not retry.
* This market had taker delay enabled (`itode: true`). A `delayed` response is not a fill. The bot polls `get_order` and account trades. Amounts of zero on that response are not a position.
* A FAK order is not supposed to rest. If one is still live, the bot calls `cancel_all` for this account. Use an account that this bot is the only trader on.
* Until trade records arrive, a matched size can be valued at the limit price, which is the worst price the order was allowed to pay. Settlement PnL uses the trade cost once those records exist. `cost_basis` is `trades`, `human`/`e6`, or `limit_price_cap`.
* Fees are charged by Polymarket at match time in pUSD. They are not a field inside the signed order. Winning shares redeem for $1. Losing shares redeem for $0. The bot does not redeem; it accounts the resolution.

Open cost of unsettled fills, plus the stake reserved by an in-flight intent, cannot exceed `max_open_cost_usd` (75). One window still cannot exceed the $15 stake.

## Settlement

A position stays open until Gamma reports an official result: a closed market whose `outcomePrices` are 0 and 1, and/or `eventMetadata.finalPrice` compared with `priceToBeat`. If those two official signals disagree, the position stays open. A missing close is not a loss. The Chainlink print at the end of the window is logged as a provisional observation and is not the PnL.

On startup the bot reloads every unresolved position, including ones older than 15 minutes, and keeps asking Gamma. Official results are retried every 15 seconds for the first 30 minutes after the window, then every 60 seconds, with no cutoff. `UNKNOWN` and feed-only rows already in `trades.csv` are not counted as realized PnL.

The price-to-beat used for a new entry must have been received live from the RTDS TWAP at the start second. A late backfill does not make the window tradable. A hole in the Chainlink spot series longer than `max_feed_lag_s` (6 seconds) blocks that window for the rest of the window. A stale book blocks entry as well.

## Install and run with Python

Use Python 3.12 from python.org (3.11 or newer; `polymarket-client` requires it). Open `D:\python\Grok_Polymarket` in VS Code if you want that editor, then use Terminal → New Terminal. The commands call `python` directly. Do not activate the virtual environment and do not run a shell setup script.

Create the folder and clone once:

```text
cd D:\python
git clone https://github.com/AlgoTrader007/Grok_PolymarketBot.git Grok_Polymarket
cd D:\python\Grok_Polymarket
```

Install:

```text
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "import shutil; shutil.copyfile('.env.example', '.env')"
```

Skip the copy command if `.env` already exists. Live trading is still off. On a machine where `py` is not the launcher, use `python -m venv .venv` and then `.venv\Scripts\python.exe` the same way. The Linux form of the same steps is `python3 -m venv .venv` and `.venv/bin/python`.

Check the local configuration. This does not contact the exchange and does not send an order:

```text
cd D:\python\Grok_Polymarket
.\.venv\Scripts\python.exe bot.py --check
```

A default checkout prints `mode: paper` and `orders: not sent`.

Run in the foreground. This is the supported way to run it. Leave the terminal open.

```text
cd D:\python\Grok_Polymarket
.\.venv\Scripts\python.exe -u bot.py
```

Stop that process with Ctrl+C in the same terminal. The process saves `logs\execution_state.json`, and if a live client was connected it requests cancellation of resting orders, then exits. Ctrl+C does not turn live mode on and does not delete logs or `.env`. A hard close of the terminal can skip that cancel request; use Ctrl+C when you want the graceful stop. The process does not restart itself.

This repository does not register a Windows service, a Task Scheduler job, or a restart loop. Closing the terminal stops the bot until you start `bot.py` again yourself.

Status and logs, from a second terminal while `bot.py` is running:

```text
cd D:\python\Grok_Polymarket
.\.venv\Scripts\python.exe view.py
.\.venv\Scripts\python.exe -c "print(open(r'logs\state.json', encoding='utf-8').read())"
```

`view.py` reads `logs\state.json` beside `view.py`, not whatever directory the shell happens to be in. Ctrl+C stops only the viewer. `bot.py` keeps running. The clock is the `timezone` in `config.toml` (currently America/Los_Angeles) and is shown with that timestamp's numeric UTC offset. It is not relabeled PT or converted to the Windows time zone. Windows does not ship that zone database; `tzdata` in `requirements.txt` supplies it.

If the picture stops changing, check the file from a new terminal:

```text
py view.py --diag
```

`writer: advancing` means `bot.py` is still updating the file, so a frozen window is the viewer or a paused console. `writer: mtime did not change` together with `pid_alive: True` means that process is up but its state writer is not updating the file. `pid_alive: False` means that process is gone. The command prints the absolute path, the pid inside the file, and the file's age. It does not read environment variables and does not send an order.

Selecting text in a Windows console can pause the program that owns that window until you press Esc or Enter. `--diag` is a separate process, so it can still see the file while the viewer window is paused. A screenshot of a still frame does not by itself show which of these happened.

Report, using a calendar day in America/Los_Angeles:

```text
.\.venv\Scripts\python.exe risk_report.py --day 2026-11-10
```

The report is also written to `reports\2026-11-10.md`. It does not place orders and does not enable live mode.

## Live PnL chart

Run this in a third terminal while `bot.py` is running. It only reads `logs\trades.csv` and `logs\state.json` next to the script. It does not read `.env` and it does not send orders. Ctrl+C closes the chart and leaves the bot running. Times follow `timezone` in `config.toml` (currently America/Los_Angeles), not the Windows clock.

```text
.\.venv\Scripts\python.exe pnl_chart.py
.\.venv\Scripts\python.exe pnl_chart.py --day
.\.venv\Scripts\python.exe pnl_chart.py --since 2026-11-01 --interval 5
.\.venv\Scripts\python.exe pnl_chart.py --png pnl.png
.\.venv\Scripts\python.exe pnl_chart.py --web
```

`--day` with no date is today in the config timezone. `--png` writes one image and exits. `--web` serves the same image at `http://127.0.0.1:8765/` on that machine only; open it in a browser on the Remote Desktop session. If the desktop window cannot open, use `--web`. A state file older than 5 seconds is marked STALE. A locked or half-written file is retried, and the chart keeps the last read that succeeded. Official settlements are the only rows on the realized line. Open positions are marked only when the state file has a bid or an explicit mark for them.

## Environment names

Secrets are read from the process environment and, if present, from a local `.env` file. Existing process variables win over `.env`. The file is gitignored. Do not commit it, and do not paste key values into the repository, a pull request, or a chat.

| Name | Purpose |
|---|---|
| `POLYBTC_LIVE_CONFIRM` | Must be exactly `YES` or live mode stays off |
| `POLYMARKET_PRIVATE_KEY` | Signer private key for the official SDK |
| `POLYMARKET_WALLET_ADDRESS` | Polymarket account wallet that holds pUSD |
| `POLYMARKET_RELAYER_API_KEY` | Optional. Gasless relayer only |
| `POLYMARKET_RELAYER_API_KEY_ADDRESS` | Optional. Relayer key address |
| `POLYMARKET_BUILDER_API_KEY` | Optional. Builder wallet deployment only |
| `POLYMARKET_BUILDER_SECRET` | Optional. Builder secret |
| `POLYMARKET_BUILDER_PASSPHRASE` | Optional. Builder passphrase |
| `POLYBTC_KILL` | Set to `1` to latch the kill switch |

Wallet types are chosen by the SDK, not by a hard-coded signature number. Accounts created on polymarket.com on or after 2026-05-04 use a deposit wallet (signature type 3, POLY_1271). Older email or Google accounts use a proxy wallet (type 1). MetaMask or Rabby accounts use a Safe (type 2). `POLYMARKET_WALLET_ADDRESS` is that account wallet, copied from the Polymarket profile, not a newly guessed address. The private key is the signer for that wallet. Relayer and builder keys are not required to place CLOB orders for an existing wallet. This bot will not deploy a wallet for you.

Logs pass through redaction for those secret values. Do not point the SDK at a custom host. The geoblock URL is fixed at `https://polymarket.com/api/geoblock`.

## Turn live mode on

Both of these are required. Either one alone does nothing.

1. In `config.toml`, set `enabled = true` under `[live]`.
2. In the local `.env`, set `POLYBTC_LIVE_CONFIRM=YES` and the key and wallet variables.

Then start `bot.py` in the foreground, as above. Before any order the bot also requires:

* no `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or SOCKS proxy variables (a proxy is a failed closed check, not something the bot routes around)
* `GET https://polymarket.com/api/geoblock` returns `blocked: false` and a country other than `US` or `UM`
* `get_closed_only_mode()` is false
* the kill file `logs\KILL` is absent and `POLYBTC_KILL` is unset

If any check fails, no new order is sent. The process can still settle positions and cancel. A refused geoblock check is final for new entries until a later check passes. This host used while writing the code was geoblocked (`blocked: true`, country `US`); that is expected and is not bypassed.

There is no paper fill while live mode was requested but blocked. That avoids a paper position that looks like an order.

## Stop, cancel, and limits

Ctrl+C in the `bot.py` terminal is the stop command. To refuse new entries even after the next start, latch the kill file from any terminal. This does not start the bot and does not send an order:

```text
cd D:\python\Grok_Polymarket
.\.venv\Scripts\python.exe bot.py --kill
```

A running `bot.py` sees `logs\KILL` and stops new entries. It still exits only when that terminal gets Ctrl+C. Ctrl+C then saves state and, when a live client is connected, requests cancellation of resting orders.

Cancel resting orders without starting a trading loop. This needs the local key and wallet, places nothing, and also writes `logs\KILL`. If those variables are missing it exits without sending a cancel:

```text
.\.venv\Scripts\python.exe bot.py --cancel-all
```

Allow entries on a later start only when you mean to. This does not start the bot and does not set `POLYBTC_LIVE_CONFIRM`:

```text
.\.venv\Scripts\python.exe bot.py --clear-kill
```

Risk limits in `config.toml`: price 0.70, edge 0.05 after fees, $15 stake, one side per window, at most two attempts and only after a confirmed zero fill, and $75 of open cost. The probability model is not a risk limit and was not refit.

## Update from GitHub without losing logs or secrets

`logs\`, `reports\`, `.env`, and `.venv\` are gitignored.

```text
cd D:\python\Grok_Polymarket
.\.venv\Scripts\python.exe bot.py --kill
```

Press Ctrl+C in the terminal that is running `bot.py`. Then:

```text
git status
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bot.py --check
.\.venv\Scripts\python.exe -u bot.py
```

Leave the kill file in place if the next start must not enter. Remove it first with `python bot.py --clear-kill` only when you want entries again. `git pull` does not delete ignored logs or `.env`. If `git status` shows local edits to tracked files, resolve those before pulling. Do not copy `.env` into a commit. After a pull, `--check` should still show `mode: paper` unless you set live mode yourself. A pulled `config.toml` keeps `enabled = false`.

## Layout

| Piece | Role |
|---|---|
| `model.py` | Unchanged fair P(Up). Driftless GBM with a 60-second TWAP. |
| `bot.py` | Public feeds, gates, paper entries, official settlement, live submit loop |
| `gates.py` | Edge, tick, depth, staleness, one-side, and geoblock decisions |
| `live_exec.py` | Official SDK adapter, durable intents, reconcile, cancel |
| `settlement.py` | Official outcome and restart restore |
| `view.py` | Reads `logs/state.json` |
| `risk_report.py` | Official rows only. Green week does not enable live mode |

Logs in `logs/`: `signals.jsonl`, `trades.jsonl`, `trades.csv`, `ticks.jsonl`, `events.jsonl`, `state.json`, and `execution_state.json` (intents and open positions). Timestamps are America/Los_Angeles.

## Tests

`python -m pytest -q` covers config and geoblock refusal, edge and price and stake, stale data, both-side blocking, partial and rejected and ambiguous submits, duplicate prevention across a restart, official settlement retry, and the Python-only command docs. The order API is mocked. GitHub Actions runs that suite on `windows-latest` and `ubuntu-latest` with Python 3.12.

Not tested, on purpose: no funded account, no real order, no cancel against the production CLOB, no live geoblock pass from an allowed country, and no claim that the model would have made money. A FAK partial caused by the book moving is implemented and mocked; the production matching engine was not exercised.
