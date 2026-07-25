import os
import unittest
from unittest.mock import patch

import email_register


class VerificationCodeTimeoutTests(unittest.TestCase):
    def test_tmail_uses_120_second_default(self):
        with (
            patch.dict(os.environ, {"GROK_REGISTER_MAIL_CODE_TIMEOUT": ""}),
            patch.dict(email_register._conf, {"verification_code_timeout": 120}),
            patch(
                "email_register.wait_for_verification_code",
                return_value="ABC-123",
            ) as wait,
        ):
            code = email_register.get_oai_code("mail-token", "user@example.com")

        self.assertEqual(code, "ABC123")
        self.assertEqual(wait.call_args.kwargs["timeout"], 120)

    def test_environment_can_override_timeout(self):
        with (
            patch.dict(os.environ, {"GROK_REGISTER_MAIL_CODE_TIMEOUT": "90"}),
            patch(
                "email_register.wait_for_verification_code",
                return_value=None,
            ) as wait,
        ):
            email_register.get_oai_code("mail-token", "user@example.com")

        self.assertEqual(wait.call_args.kwargs["timeout"], 90)


if __name__ == "__main__":
    unittest.main()
