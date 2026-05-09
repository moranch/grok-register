"""MoeMailMailbox — register into unified registry."""
from core._vendor_aar.base_mailbox import MoeMailMailbox  # noqa: F401
from _vendor_aar.providers.registry import register_provider

register_provider("mailbox", "moemail_api")(MoeMailMailbox)
