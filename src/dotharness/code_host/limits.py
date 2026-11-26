"""Shared limit constants for the code host.

This module is the single home of the change-request body limit. It imports nothing, so ``sisyphus`` may import it
without a cycle (``code_host`` never imports ``sisyphus``).
"""

# The change-request body limit in Unicode code points. The reference host declares it as
# ``limits.max_change_request_body_chars``; ``sisyphus.prbody.MAX_BODY_CHARS`` re-exposes it.
DEFAULT_MAX_CHANGE_REQUEST_BODY_CHARS = 60000
