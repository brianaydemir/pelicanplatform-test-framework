# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Back the origin with the Pelican store (encrypted blocks plus an
# embedded catalog). Requires v26.0.0-rc.0 or later.
#
# init-data.sh cannot prime it; upload with `pelican object put`. See
# "Test data" in the README.

ORIGIN_VARIANT=pstore
ORIGIN_MEM=768M
