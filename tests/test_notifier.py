import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notifier


URL = "https://pergamer.com/market/Patchwork%20Protector?game=570"


class ParserTests(unittest.TestCase):
    def test_normalizes_persian_digits_and_separators(self):
        self.assertEqual(notifier.parse_decimal("۳٬۵۵۱٬۱۰۰", "price"), 3551100)
        self.assertEqual(notifier.parse_decimal("٣,٥٥١,١٠٠", "price"), 3551100)

    def test_parses_and_canonicalizes_product_url(self):
        product = notifier.parse_product_url(URL)
        self.assertEqual(product.market_hash_name, "Patchwork Protector")
        self.assertEqual(product.game, 570)
        self.assertEqual(product.url, URL)

    def test_rejects_other_hosts(self):
        with self.assertRaises(notifier.NotifierError):
            notifier.parse_product_url("https://example.com/market/item?game=570")

    def test_converts_usdt_cent_to_irr(self):
        rates = [{"top_asset": "USDTCent", "bottom_asset": "IRT", "rate": "26700"}]
        self.assertEqual(notifier.convert_to_irr("133", "USDTCent", rates), 3551100)

    @patch("notifier.read_json")
    def test_wallex_rate_matches_site_rounding(self, read_json):
        read_json.return_value = {
            "result": {
                "symbols": {
                    "USDTTMN": {"stats": {"askPrice": "266758.0000000000000000"}}
                }
            }
        }
        rates = notifier.fetch_https_rates()
        self.assertEqual(rates[0]["rate"], "26700")
        self.assertEqual(notifier.convert_to_irr("133", "USDTCent", rates), 3551100)

    @patch("notifier.fetch_wallex_toman", side_effect=notifier.NotifierError("blocked"))
    @patch("notifier.fetch_tetherland_toman", return_value=notifier.Decimal("266550"))
    def test_https_rate_uses_backup_provider(self, _, __):
        rates = notifier.fetch_https_rates()
        self.assertEqual(rates[0]["rate"], "26700")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "prices.json"
        self.product = notifier.parse_product_url(URL)
        self.sent = []

    def tearDown(self):
        self.temp.cleanup()

    def sender(self, message, token, chat_id):
        self.sent.append(message)

    def run_price(self, value, sender=None):
        return notifier.process_products(
            [self.product],
            self.state_path,
            scraper=lambda _: notifier.Price("Patchwork Protector", value),
            sender=sender or self.sender,
        )

    def test_first_price_is_silent_baseline(self):
        self.assertEqual(self.run_price(3551100), 0)
        self.assertEqual(self.sent, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["products"][URL]["price_irr"], 3551100)

    def test_unchanged_price_is_silent(self):
        self.run_price(3551100)
        self.assertEqual(self.run_price(3551100), 0)
        self.assertEqual(self.sent, [])

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-1001"})
    def test_changed_price_alerts_and_updates_state(self):
        self.run_price(3551100)
        self.assertEqual(self.run_price(3600000), 0)
        self.assertIn("Price UP", self.sent[0])
        self.assertIn("Old: 3,551,100 IRR", self.sent[0])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["products"][URL]["price_irr"], 3600000)

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-1001"})
    def test_delivery_failure_preserves_previous_price(self):
        self.run_price(3551100)

        def fail(*_):
            raise notifier.NotifierError("delivery failed")

        self.assertEqual(self.run_price(3600000, sender=fail), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["products"][URL]["price_irr"], 3551100)

    def test_scrape_failure_preserves_previous_price(self):
        self.run_price(3551100)

        def fail(_):
            raise notifier.NotifierError("empty")

        self.assertEqual(
            notifier.process_products([self.product], self.state_path, scraper=fail, sender=self.sender),
            1,
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["products"][URL]["price_irr"], 3551100)

    def test_corrupt_state_never_becomes_new_baseline(self):
        self.state_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(notifier.NotifierError):
            notifier.process_products(
                [self.product],
                self.state_path,
                scraper=lambda _: notifier.Price("Patchwork Protector", 1),
                sender=self.sender,
            )

    def test_wrong_state_shape_never_becomes_new_baseline(self):
        self.state_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(notifier.NotifierError):
            self.run_price(3551100)


if __name__ == "__main__":
    unittest.main()
