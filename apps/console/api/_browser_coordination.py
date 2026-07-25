"""Coordinate long-lived browser sessions owned by Console runtimes."""
from __future__ import annotations

import threading


# Registration runs in a subprocess while historical CPA recovery runs in
# worker threads.  DrissionPage/Chromium sessions in the same container must
# not overlap: one session quitting can invalidate the other's CDP page.
BROWSER_SESSION_LOCK = threading.Lock()

# Set before the supervisor waits for an active CPA browser to finish.  CPA
# workers check this while holding their own FIFO lock, which prevents another
# recovery browser from jumping ahead of a queued registration task.
REGISTRATION_BROWSER_PENDING = threading.Event()

