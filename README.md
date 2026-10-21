# Per Price Notifier

Python price tracker for product pages on Pergamer.

Planned behavior:

- Read an editable list of product URLs.
- Run every 30 minutes with GitHub Actions.
- Track the first active seller row and its price in Iranian rials (IRR).
- Save the latest observed price as persistent state.
- Stay silent when establishing the first baseline or when the price is unchanged.
- Send a Telegram notification when the price rises or falls.

This project monitors prices only. It does not place orders or make purchases.
