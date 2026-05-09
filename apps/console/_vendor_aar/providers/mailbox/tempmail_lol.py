"""TempMail.lol — register into unified registry."""
from core._vendor_aar.base_mailbox import TempMailLolMailbox  # noqa: F401
from _vendor_aar.providers.registry import register_provider

register_provider("mailbox", "tempmail_lol_api")(TempMailLolMailbox)
