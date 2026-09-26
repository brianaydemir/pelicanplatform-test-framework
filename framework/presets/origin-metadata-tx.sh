# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# `origin-metadata` in transactional mode: an upload succeeds only if its
# event is delivered, and a failed delivery rolls the object back.

ORIGIN_METADATA=true
ORIGIN_METADATA_MODE=transactional

fed_enable_profile metadata
