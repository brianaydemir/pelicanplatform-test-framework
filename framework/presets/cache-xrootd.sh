# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put XRootD in front of the caches, as the OSDF runs them. Add
# `origin-xrootd` for the origins too.

CACHE_VARIANT=xrootd
