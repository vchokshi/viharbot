"""Founder-run laptop check for retest R1: can THIS machine reach the throwaway database?

THROWAWAY. Run it from your own laptop (an address that is NOT a trusted source of the cluster),
once BEFORE the trusted-source rule exists (expected: it connects, which documents the default
open state) and again AFTER the rule is added (expected: it cannot). It makes the same staged
attempt as the outsider app: DNS, TCP, the PostgreSQL TLS handshake, then a login, and prints one
JSON object that says where it stopped. It never prints the host, the user or the password.

The details come from environment variables so nothing sits on a command line or in shell history:

    read -rs PROBE_LAPTOP_DB_PASSWORD; export PROBE_LAPTOP_DB_PASSWORD     # type it, no echo
    export PROBE_LAPTOP_DB_HOST=<public host of the throwaway cluster>
    export PROBE_LAPTOP_DB_USER=<throwaway user>
    export PROBE_LAPTOP_DB_PORT=25060          # optional
    export PROBE_LAPTOP_DB_NAME=defaultdb      # optional, the default
    python deploy/digitalocean-probe/probe_laptop_dbcheck.py

Optional: PROBE_LAPTOP_DB_CA_PEM (the cluster CA, public) makes the login use verify-full.
The login stage needs psycopg (`pip install "psycopg[binary]"`); without it the result says
`login_not_run` and the TCP and TLS stages still tell you whether the address is let in. The
`psql` equivalent is in RUNBOOK section 8b. The script sends nothing anywhere except to the
cluster you name. Exit status: 0 if the database was NOT reached, 1 if it was reached, 2 if the
check could not run (missing variables, or the login stage could not run).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Mapping

import probe_outsider


def main(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    mapped = {
        "PROBE_OUTSIDER_DB_HOST": env.get("PROBE_LAPTOP_DB_HOST", ""),
        "PROBE_OUTSIDER_DB_PORT": env.get("PROBE_LAPTOP_DB_PORT", ""),
        "PROBE_OUTSIDER_DB_NAME": env.get("PROBE_LAPTOP_DB_NAME", ""),
        "PROBE_OUTSIDER_DB_USER": env.get("PROBE_LAPTOP_DB_USER", ""),
        "PROBE_OUTSIDER_DB_PASSWORD": env.get("PROBE_LAPTOP_DB_PASSWORD", ""),
        "PROBE_OUTSIDER_DB_CA_PEM": env.get("PROBE_LAPTOP_DB_CA_PEM", ""),
    }
    result = probe_outsider.database_check(mapped)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["classification"] in ("not_configured", "login_not_run"):
        return 2
    return 1 if result["reached_database"] else 0


if __name__ == "__main__":
    sys.exit(main())
