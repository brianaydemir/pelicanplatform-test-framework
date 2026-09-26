# shellcheck shell=sh
#
# Add a second origin, origin-1, that exports the same prefixes as
# origin-0, with the same key: a replica, not a second owner (see
# `topo-multi-owner` for that). fed.sh pins both to origin-0's issuer in
# framework/var/generated/conf/60-origin-exports.yaml. The director may
# send any request to either.

fed_enable_profile multi-origin
