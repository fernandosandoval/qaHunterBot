"""
ssl_setup.py
Makes Python trust the operating system certificate store instead of the bundled `certifi` list.

Why: antivirus HTTPS scanners (Avast/AVG Web Shield, Kaspersky, ESET, corporate proxies...) re-sign
traffic with their own root certificate, which Windows trusts but `certifi` does not. That makes
every request fail with "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate".

Certificate verification stays ENABLED: we only change WHERE trusted roots come from.
Import this module before any network library is used (it is idempotent).
Set USE_SYSTEM_CERTS=false in .env to opt out.
"""

from __future__ import annotations

import os
import sys

_done = False

try:  # allow USE_SYSTEM_CERTS to be set in .env
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def enable_system_certs() -> bool:
    """Injects `truststore` into the ssl module. Returns True if the system store is active."""
    global _done
    if _done:
        return True
    if os.getenv("USE_SYSTEM_CERTS", "true").strip().lower() in ("false", "0", "no"):
        return False
    try:
        import truststore

        truststore.inject_into_ssl()
        _done = True
        return True
    except ImportError:
        print(
            "[WARN] Paquete 'truststore' no instalado; se usan los certificados de certifi "
            "(pip install -r requirements.txt).",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[WARN] No se pudo activar el almacen de certificados del sistema: {exc}", file=sys.stderr)
    return False


enable_system_certs()
