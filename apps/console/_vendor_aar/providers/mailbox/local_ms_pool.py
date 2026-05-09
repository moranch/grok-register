"""Local Microsoft mailbox pool — register into unified registry."""
from core._vendor_aar.local_ms_mailbox import LocalMicrosoftMailboxPool  # noqa: F401
from _vendor_aar.providers.registry import register_provider

register_provider("mailbox", "local_ms_pool")(LocalMicrosoftMailboxPool)
