#!/usr/bin/env python3
"""Track lowest active Pergamer listing prices and notify through Telegram."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from websockets.sync.client import connect

API_BASE = "https://api.pergamer.com"
WEBSOCKET_URL = "wss://api.pergamer.com/ws/user/"
WALLEX_MARKETS_URL = "https://api.wallex.ir/v1/markets"
TETHERLAND_CURRENCIES_URL = "https://api.tetherland.com/currencies"
BITPIN_MARKETS_URL = "https://api.bitpin.org/v1/mkt/markets/"
SITE_ORIGIN = "https://pergamer.com"
USER_AGENT = "Per-Price-Notifier/1.0 (+https://github.com/mortezamim/Per-Price-Notifier)"
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
class NotifierError(RuntimeError):
    """Expected runtime error that must not replace valid state."""


@dataclass(frozen=True)
class Product:
    url: str
    market_hash_name: str
    game: int


@dataclass(frozen=True)
class Price:
    name: str
    irr: int


def normalize_number(value: object) -> str:
    """Normalize Persian/Arabic digits and common thousands separators."""
    return (
        str(value)
        .translate(PERSIAN_DIGITS)
        .replace(",", "")
        .replace("٬", "")
        .replace("،", "")
        .replace(" ", "")
        .strip()
    )


def parse_decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(normalize_number(value))
    except (InvalidOperation, ValueError) as exc:
        raise NotifierError(f"Invalid {label}: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise NotifierError(f"Invalid {label}: value must be positive")
    return number


def parse_product_url(raw_url: str) -> Product:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"pergamer.com", "www.pergamer.com"}:
        raise NotifierError(f"Unsupported product URL: {url}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "market" or not parts[1]:
        raise NotifierError(f"Expected a Pergamer /market/<product> URL: {url}")
    game_values = parse_qs(parsed.query).get("game", [])
    if len(game_values) != 1 or not game_values[0].isdigit():
        raise NotifierError(f"Product URL needs one numeric game parameter: {url}")
    from urllib.parse import unquote

    name = unquote(parts[1]).strip()
    if not name:
        raise NotifierError(f"Product URL has an empty product name: {url}")
    canonical = f"https://pergamer.com/market/{quote(name, safe='')}?game={game_values[0]}"
    return Product(canonical, name, int(game_values[0]))


def load_products(path: Path) -> list[Product]:
    if not path.is_file():
        raise NotifierError(f"Product file not found: {path}")
    products: list[Product] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            product = parse_product_url(value)
        except NotifierError as exc:
            raise NotifierError(f"{path}:{line_number}: {exc}") from exc
        if product.url in seen:
            raise NotifierError(f"{path}:{line_number}: duplicate product URL")
        seen.add(product.url)
        products.append(product)
    if not products:
        raise NotifierError(f"No product URLs found in {path}")
    return products


def read_json(url: str, timeout: int = 20, origin: str | None = SITE_ORIGIN) -> object:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if origin is not None:
        headers["Origin"] = origin
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NotifierError(f"Request failed for {url}: {type(exc).__name__}") from exc


def fetch_rates(timeout: int = 15) -> list[dict[str, object]]:
    try:
        with connect(
            WEBSOCKET_URL,
            origin=SITE_ORIGIN,
            open_timeout=timeout,
            close_timeout=2,
            user_agent_header=USER_AGENT,
        ) as websocket:
            for _ in range(10):
                message = json.loads(websocket.recv(timeout=timeout))
                if message.get("event") == "wallet.asset_rates" and isinstance(
                    message.get("asset_rates"), list
                ):
                    return message["asset_rates"]
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f" HTTP {status}" if status is not None else ""
        raise NotifierError(
            f"Asset-rate WebSocket failed: {type(exc).__name__}{detail}"
        ) from exc
    raise NotifierError("Asset-rate WebSocket returned no rates")


def fetch_wallex_toman() -> Decimal:
    payload = read_json(WALLEX_MARKETS_URL, origin=None)
    try:
        ask_price = payload["result"]["symbols"]["USDTTMN"]["stats"]["askPrice"]
    except (KeyError, TypeError) as exc:
        raise NotifierError("Wallex USDTTMN ask price is unavailable") from exc
    return parse_decimal(ask_price, "Wallex USDTTMN ask price")


def fetch_tetherland_toman() -> Decimal:
    payload = read_json(TETHERLAND_CURRENCIES_URL, origin=None)
    try:
        price = payload["data"]["currencies"]["USDT"]["price"]
    except (KeyError, TypeError) as exc:
        raise NotifierError("Tetherland USDT price is unavailable") from exc
    return parse_decimal(price, "Tetherland USDT price")


def fetch_bitpin_toman() -> Decimal:
    payload = read_json(BITPIN_MARKETS_URL, origin=None)
    try:
        markets = payload["results"]
    except (KeyError, TypeError) as exc:
        raise NotifierError("Bitpin markets are unavailable") from exc
    if not isinstance(markets, list):
        raise NotifierError("Bitpin markets are unavailable")
    for market in markets:
        if isinstance(market, dict) and market.get("code") == "USDT_IRT":
            return parse_decimal(market.get("price"), "Bitpin USDT/IRT price")
    raise NotifierError("Bitpin USDT/IRT price is unavailable")


def fetch_https_rates() -> list[dict[str, object]]:
    """Mirror Pergamer's whole-thousand-toman USDT quote over runner-safe HTTPS."""
    failures: list[str] = []
    toman_per_usdt = None
    for name, provider in (
        ("Wallex", fetch_wallex_toman),
        ("Tetherland", fetch_tetherland_toman),
        ("Bitpin", fetch_bitpin_toman),
    ):
        try:
            toman_per_usdt = provider()
            break
        except NotifierError as exc:
            failures.append(f"{name}: {exc}")
    if toman_per_usdt is None:
        raise NotifierError("All HTTPS rate providers failed (" + "; ".join(failures) + ")")
    rounded_toman = (
        toman_per_usdt / Decimal("1000")
    ).to_integral_value(rounding=ROUND_CEILING) * Decimal("1000")
    irr_per_usdt_cent = rounded_toman / Decimal("10")
    return [
        {
            "top_asset": "USDTCent",
            "bottom_asset": "IRT",
            "rate": str(irr_per_usdt_cent),
        }
    ]


def convert_to_irr(price: object, unit: object, rates: list[dict[str, object]]) -> int:
    amount = parse_decimal(price, "listing price")
    unit_name = str(unit)
    if unit_name == "IRT":
        converted = amount
    else:
        converted = None
        for rate in rates:
            top = rate.get("top_asset")
            bottom = rate.get("bottom_asset")
            if top == unit_name and bottom == "IRT":
                converted = amount * parse_decimal(rate.get("rate"), "asset rate")
                break
            if top == "IRT" and bottom == unit_name:
                converted = amount / parse_decimal(rate.get("rate"), "asset rate")
                break
        if converted is None:
            raise NotifierError(f"No {unit_name}/IRT conversion rate")
    value = int(converted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if value <= 0:
        raise NotifierError("Converted listing price is not positive")
    return value


def scrape_price(product: Product) -> Price:
    endpoint = (
        f"{API_BASE}/api/market/{quote(product.market_hash_name, safe='')}/"
        f"?game={product.game}"
    )
    payload = read_json(endpoint)
    if not isinstance(payload, dict):
        raise NotifierError("Market response is not an object")
    listings = payload.get("listings")
    if not isinstance(listings, list) or not listings:
        raise NotifierError("No active seller listings found")
    name = str(payload.get("market_name") or payload.get("market_hash_name") or product.market_hash_name)
    try:
        rates = fetch_rates()
    except NotifierError as websocket_error:
        print(f"warning: {websocket_error}; using HTTPS rate fallback", file=sys.stderr)
        rates = fetch_https_rates()
    converted: list[int] = []
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        try:
            converted.append(convert_to_irr(listing.get("price"), listing.get("price_unit"), rates))
        except NotifierError:
            continue
    if not converted:
        raise NotifierError("No active listing had a valid IRR price")
    return Price(name=name, irr=min(converted))


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "products": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotifierError(f"State file is unreadable; refusing baseline reset: {path}") from exc
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("products"), dict)
    ):
        raise NotifierError(f"State file has unsupported structure: {path}")
    return state


def save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temporary:
            json.dump(state, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def format_money(irr: int) -> str:
    return f"{irr:,} IRR ({irr // 10:,} toman)"


def build_message(name: str, old_price: int, new_price: int, url: str) -> str:
    direction = "UP" if new_price > old_price else "DOWN"
    return (
        f"Price {direction}\n"
        f"{html.escape(name)}\n"
        f"Old: {format_money(old_price)}\n"
        f"New: {format_money(new_price)}\n"
        f'<a href="{html.escape(url, quote=True)}">Open product</a>'
    )


def send_telegram(message: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        raise NotifierError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for alerts")
    from urllib.parse import urlencode

    body = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NotifierError(f"Telegram delivery failed: {type(exc).__name__}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise NotifierError("Telegram rejected alert")


def process_products(
    products: list[Product],
    state_path: Path,
    scraper: Callable[[Product], Price] = scrape_price,
    sender: Callable[[str, str, str], None] = send_telegram,
) -> int:
    state = load_state(state_path)
    stored = state["products"]
    assert isinstance(stored, dict)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    failures = 0
    for product in products:
        try:
            current = scraper(product)
            previous = stored.get(product.url)
            old_price = previous.get("price_irr") if isinstance(previous, dict) else None
            if old_price is not None and (not isinstance(old_price, int) or old_price <= 0):
                raise NotifierError(f"Invalid stored price for {product.url}; refusing reset")
            if old_price == current.irr:
                print(f"unchanged: {current.name}: {current.irr} IRR")
                continue
            if old_price is not None:
                sender(build_message(current.name, old_price, current.irr, product.url), token, chat_id)
                print(f"alerted: {current.name}: {old_price} -> {current.irr} IRR")
            else:
                print(f"baseline: {current.name}: {current.irr} IRR (silent)")
            stored[product.url] = {
                "name": current.name,
                "price_irr": current.irr,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state_path, state)
        except Exception as exc:
            failures += 1
            print(f"error: {product.url}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", type=Path, default=Path("products.txt"))
    parser.add_argument("--state", type=Path, default=Path("prices.json"))
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="read live prices without Telegram or state changes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        products = load_products(args.products)
        if args.check_only:
            for product in products:
                price = scrape_price(product)
                print(f"{price.name}: {format_money(price.irr)} | {product.url}")
            return 0
        return process_products(products, args.state)
    except NotifierError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
