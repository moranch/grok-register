from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from api import mailboxes


class MailboxCredentialApiTests(unittest.TestCase):
    def test_authenticated_request_returns_one_no_store_credential(self):
        pool = SimpleNamespace(
            export_credential=lambda email: (
                "user@outlook.com----password----client-id----refresh-token"
            )
        )
        response = Response()
        with (
            patch.object(mailboxes, "check_auth") as check_auth,
            patch.object(mailboxes, "_hotmail_row", return_value={"id": 7}),
            patch.object(mailboxes, "_hotmail_pool_from_row", return_value=pool),
        ):
            result = mailboxes.api_hotmail_credential(
                request=SimpleNamespace(),
                response=response,
                mbox_id=7,
                payload=mailboxes.HotmailCredentialRequest(email="user@outlook.com"),
            )

        check_auth.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["credential"],
            "user@outlook.com----password----client-id----refresh-token",
        )
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")

    def test_unknown_account_returns_404(self):
        def missing(_email: str) -> str:
            raise KeyError("account not found")

        pool = SimpleNamespace(export_credential=missing)
        with (
            patch.object(mailboxes, "check_auth"),
            patch.object(mailboxes, "_hotmail_row", return_value={"id": 7}),
            patch.object(mailboxes, "_hotmail_pool_from_row", return_value=pool),
            self.assertRaises(HTTPException) as raised,
        ):
            mailboxes.api_hotmail_credential(
                request=SimpleNamespace(),
                response=Response(),
                mbox_id=7,
                payload=mailboxes.HotmailCredentialRequest(email="missing@outlook.com"),
            )

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
