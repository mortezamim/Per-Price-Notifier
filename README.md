# Per Price Notifier

Python price tracker for Pergamer product pages. It monitors only: no buying,
orders, wallet access, or delivery-time logic.

## Behavior

- Reads one product URL per line from [`products.txt`](products.txt).
- Fetches active seller listings from Pergamer's public market API.
- Uses Pergamer's public live asset-rate WebSocket to convert listing units to
  Iranian rials (IRR), matching the site's displayed listing price.
- Selects the lowest active-seller price (the first row in the sorted lower
  **All active sellers** section). Headline/history prices and wallet balance
  are ignored.
- First valid scrape creates a silent baseline. Unchanged prices stay silent.
- Price increases and decreases send old price, new price, and product link to
  Telegram. There is no threshold.
- Failed, empty, unavailable, or zero-price extraction preserves last valid
  state and fails the workflow.

## Product list

Edit `products.txt`; keep one full Pergamer market URL per line:

```text
https://pergamer.com/market/Patchwork%20Protector?game=570
```

Blank lines and lines starting with `#` are ignored. Other products using the
same Pergamer URL shape work without parser changes. Persian and Arabic digits
and common thousands separators are normalized; stored prices remain integer
IRR. A newly added product establishes its own silent baseline.

## Telegram setup

Add both repository Actions secrets under **Settings > Secrets and variables >
Actions**:

- `TELEGRAM_BOT_TOKEN`: token from BotFather.
- `TELEGRAM_CHAT_ID`: numeric group ID, commonly starting with `-100` for a
  supergroup.

The bot must be present in the target group. Send
`/start@Per_Price_Notifier_bot` in that group before discovery.

Private discovery (recommended):

```powershell
$env:TELEGRAM_BOT_TOKEN = 'token-from-BotFather'
python discover_chat.py
Remove-Item Env:TELEGRAM_BOT_TOKEN
```

The script calls `getUpdates` without an offset, so it does not acknowledge or
discard queued updates. It prints only group title and numeric chat ID, never
tokens, messages, or user details.

The manual **Discover Telegram group** workflow provides the same lookup but
requires explicit acknowledgement because this is a public repository and its
logs expose group title and ID. Prefer local discovery for privacy. If no group
appears, send the command again and rerun discovery. Bots using a webhook cannot
use `getUpdates`; this project will not remove or modify an existing webhook.

## GitHub Actions

`Price notifier` runs at minute 7 and 37 of every UTC hour and supports manual
runs. GitHub schedules can start late during load. Standard GitHub-hosted runners
are free for public repositories under GitHub's current Actions policy.

The workflow uses one serialized concurrency group and writes `prices.json` to
a dedicated `price-state` branch. This survives ephemeral runners and does not
depend on disposable Actions caches. The scheduled workflow becomes active only
after it exists on the default branch and Actions are enabled. In **Settings >
Actions > General > Workflow permissions**, allow **Read and write permissions**
so the workflow can persist the state branch.

State advances only after successful Telegram delivery. Delivery failure keeps
the old price so the next run retries. If Telegram accepts an alert but the
runner dies before the state commit/push, the retry can send a duplicate; this
is unavoidable without an external transactional store. A failed state push
fails visibly instead of silently establishing a new baseline.

## Local checks

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python notifier.py --check-only
```

`--check-only` performs live read-only extraction without Telegram or state
changes. Normal local execution uses `prices.json` unless `--state` is supplied.
