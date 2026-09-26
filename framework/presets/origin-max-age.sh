# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins send Cache-Control: max-age=30, so that a V2 cache
# fetches an object again once it has held it for 30 seconds, and serves
# whatever the origin holds by then. The blocks suite's overwrite
# scenarios check that it does.
#
# Only the `posixv2` origin sends it: fed.sh refuses the XRootD origin,
# and the pstore, ssh, httpsv2, and s3v2 origins send nothing (which the
# overwrite scenarios report). XRootD caches never revalidate.

ORIGIN_CACHE_CONTROL=max-age=30
