# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# The default origin storage: Pelican's native POSIX backend (`posixv2`),
# with no XRootD. For naming the choice explicitly; the other `origin-`
# storage presets replace it.

ORIGIN_VARIANT=posixv2
