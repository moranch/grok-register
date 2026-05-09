"""AitreMailbox — register into unified registry."""
from core._vendor_aar.base_mailbox import AitreMailbox  # noqa: F401
from _vendor_aar.providers.registry import register_provider

register_provider("mailbox", "aitre_api")(AitreMailbox)
