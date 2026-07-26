from __future__ import annotations

import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

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

    def test_export_credential_returns_one_current_four_part_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text(
                "first@outlook.com----pass-1----client-1----refresh-1\n"
                "second@outlook.com----pass-2----client-2----refresh-2\n",
                encoding="utf-8",
            )
            pool = HotmailPool(path)

            self.assertEqual(
                pool.export_credential("SECOND@outlook.com"),
                "second@outlook.com----pass-2----client-2----refresh-2",
            )
            with self.assertRaises(KeyError):
                pool.export_credential("missing@outlook.com")

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

    def test_reservation_is_shared_between_pool_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text("user@outlook.com----p----c----r\n", encoding="utf-8")
            first_pool = HotmailPool(path, alias_mode="sequential", max_aliases=3)
            second_pool = HotmailPool(path, alias_mode="sequential", max_aliases=3)

            first, _ = first_pool.acquire(owner="worker-1")
            second, _ = second_pool.acquire(owner="worker-2")

            self.assertEqual(first, "user@outlook.com")
            self.assertEqual(second, "user+1@outlook.com")
            snapshot = second_pool.snapshot()
            self.assertEqual(snapshot["summary"]["reserved"], 2)
            self.assertEqual(snapshot["summary"]["available"], 1)

            first_pool.release(first, consumed=False)
            third, _ = second_pool.acquire(owner="worker-3")
            self.assertEqual(third, "user@outlook.com")

    def test_delete_only_fully_consumed_main_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text(
                "one@outlook.com----p----c----r\n"
                "two@outlook.com----p----c----r\n",
                encoding="utf-8",
            )
            pool = HotmailPool(path, alias_mode="sequential", max_aliases=2)
            one, _ = pool.acquire()
            pool.release(one, consumed=True)
            one_alias, _ = pool.acquire()
            pool.release(one_alias, consumed=True)

            result = pool.delete_used_accounts()

            self.assertEqual(result, {"deleted": 1, "remaining": 1})
            self.assertEqual(load_credentials(path)[0]["email"], "two@outlook.com")

    def test_verification_status_tracks_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text("user@outlook.com----p----c----r\n", encoding="utf-8")
            pool = HotmailPool(path, alias_mode="sequential", max_aliases=2)
            alias, _ = pool.acquire()
            self.assertEqual(pool.verification_status(alias)["item"]["status"], "reserved")
            pool.release(alias, consumed=False)
            self.assertEqual(pool.verification_status(alias)["item"]["status"], "released")

    def test_scan_host_finds_xai_code_in_outlook_junk_folder(self):
        message = EmailMessage()
        message["From"] = "SpaceXAI <noreply@x.ai>"
        message["To"] = "user@outlook.com"
        message["Subject"] = "SpaceXAI confirmation code: ABC-123"
        message.set_content("Your verification code is ABC-123")

        class FakeImap:
            selected = ""

            def __init__(self, *_args, **_kwargs):
                pass

            def authenticate(self, *_args, **_kwargs):
                return "OK", []

            def list(self):
                return "OK", [
                    b'(\\HasNoChildren) "/" INBOX',
                    b'(\\HasNoChildren \\Junk) "/" "Junk Email"',
                ]

            def select(self, mailbox, readonly=False):
                self.selected = mailbox
                return "OK", [b"1"]

            def search(self, *_args):
                if self.selected == '"Junk Email"':
                    return "OK", [b"7"]
                return "OK", [b""]

            def fetch(self, *_args):
                return "OK", [(b"7 (RFC822)", message.as_bytes())]

            def close(self):
                return "OK", []

            def logout(self):
                return "BYE", []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.txt"
            path.write_text("user@outlook.com----p----c----r\n", encoding="utf-8")
            pool = HotmailPool(path)
            with patch("core.hotmail_pool.imaplib.IMAP4_SSL", FakeImap):
                code = pool._scan_host(
                    {"email": "user@outlook.com"},
                    "user@outlook.com",
                    "access-token",
                    "outlook.office365.com",
                )
            self.assertEqual(code, "ABC-123")


if __name__ == "__main__":
    unittest.main()
