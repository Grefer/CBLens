# _*_ coding: utf-8 _*_
"""PyInstaller runtime hook: make HTTPS data sources find CA certificates."""

from __future__ import annotations

import os
import sys


if getattr(sys, "frozen", False):
    try:
        import certifi

        ca_bundle = certifi.where()
        for env_name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ.setdefault(env_name, ca_bundle)
    except Exception as exc:
        sys.stderr.write(f"[pyi_rth_network] cert setup failed: {exc!r}\n")
