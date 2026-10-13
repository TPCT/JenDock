"""One lock for every ingress change in the worker process.

The host helper serves one request at a time and DNS/Apache/TLS changes of different domains
share Apache's configuration, so applies, removals, manual issuance and the renewal loop never
run concurrently.
"""

from __future__ import annotations

import threading

INGRESS_LOCK = threading.Lock()
