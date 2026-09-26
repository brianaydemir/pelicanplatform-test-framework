# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Back the origin with the `lab-server` container over SSH, standing in
# for a site that will not run Pelican itself.

ORIGIN_VARIANT=ssh

fed_enable_profile lab
