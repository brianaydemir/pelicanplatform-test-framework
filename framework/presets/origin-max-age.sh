# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins send Cache-Control: max-age=30 (Origin.CacheControl),
# so that a cache fetches an object again once it has held it for 30
# seconds, and serves whatever the origin holds by then. The blocks
# suite's overwrite scenarios check that every origin sends it, and that
# every cache does so.

ORIGIN_CACHE_CONTROL=max-age=30
