#!/bin/sh
#
# Health check for every Pelican service. The published images define no
# HEALTHCHECK.
#
# Any HTTP response counts as healthy: this checks that the web server is
# up, not that every component is happy. Waiting for component health
# would let one unhappy service stall everything that depends on it.

set -eu

url="https://localhost:8444/api/v1.0/health"

if command -v curl >/dev/null 2>&1; then
  exec curl -sk -o /dev/null --max-time 5 "${url}"
fi

if command -v wget >/dev/null 2>&1; then
  exec wget -q -O /dev/null --no-check-certificate --timeout=5 "${url}"
fi

if command -v bash >/dev/null 2>&1; then
  exec bash -c 'exec 3<>/dev/tcp/127.0.0.1/8444'
fi

exit 1
