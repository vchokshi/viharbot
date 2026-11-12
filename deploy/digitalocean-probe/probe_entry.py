"""Container entry point: runs the component named by PROBE_ROLE (service, client, job, outsider).

The probe image has one CMD so the App Platform specs do not depend on `run_command`. Each
component sets PROBE_ROLE in its spec. THROWAWAY.
"""

from __future__ import annotations

import importlib
import os
import sys

ROLES = {"service": "probe_service", "client": "probe_client", "job": "probe_job",
         "outsider": "probe_outsider"}


def main() -> int:
    role = os.environ.get("PROBE_ROLE", "")
    if role not in ROLES:
        print(f"probe_entry: PROBE_ROLE must be one of {', '.join(sorted(ROLES))}", file=sys.stderr)
        return 2
    return int(importlib.import_module(ROLES[role]).main() or 0)


if __name__ == "__main__":
    sys.exit(main())
