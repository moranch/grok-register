from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from core.hotmail_pool import HotmailPool, extract_verification_code, load_credentials, parse_credential_line


class HotmailPoolTests(unittest.TestCase):
    def test_parse_four_field_credential(self):
        item = parse_credential_line("user@outlook.com----pass----client-id----refresh-token")
        self.assertEqual(item["email"], "user@outlook.com")
        self.assertEqual(item["client_id"], "client-id")
        self.assertEqual(item["refresh_token"], "refresh-token")

    def test_load_skips_invalid_and_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text(
                "bad\nuser@outlook.com----p----c----r\nuser@outlook.com----p2----c2----r2\n",
                encoding="utf-8",
            )
            self.assertEqual(len(load_credentials(path)), 1)

    def test_consumed_main_address_moves_to_plus_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text("user@outlook.com----p----c----r\n", encoding="utf-8")
            pool = HotmailPool(path, alias_mode="sequential", max_aliases=3)
            first, _ = pool.acquire()
            pool.release(first, consumed=True)
            second, _ = pool.acquire()
            self.assertEqual(first, "user@outlook.com")
            self.assertEqual(second, "user+1@outlook.com")

    def test_extract_xai_code(self):
        self.assertEqual(extract_verification_code("Your code is ABC-123"), "ABC-123")
        self.assertEqual(extract_verification_code("verification code: 123456"), "123456")


if __name__ == "__main__":
    unittest.main()
