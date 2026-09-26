# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Back the origin with the Pelican store (encrypted blocks plus an
# embedded catalog). Requires v26.0.0-rc.0 or later.
#
# Nothing outside the running origin can write the store, so init-data.py
# writes a plain copy of the test objects beside it, which the
# transfers suite uploads to /protected-a and /protected-b before its
# transfers. Each has storage of its own in the store, and /public, which
# takes no writes, reads /protected-a's.

ORIGIN_VARIANT=pstore
ORIGIN_MEM=768M
