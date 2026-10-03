# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the V2 origin publish an event for every object it commits,
# overwrites, or deletes, and accept uploaders' metadata (`pelican object
# put --metadata-file`, `--metadata-body`). The metadata suite tests it
# (./fed.sh test metadata).
#
# Events go to the `metadata` recorder, which saves each request in
# framework/var/data/metadata/ and passes it to `metadata-verifier`,
# Pelican's cmd/sample_metadata_server built from PELICAN_TAG's source.
# The verifier checks each event's token against the registry. The
# endpoint and retry settings are in config.d/base/40-origin.yaml.
#
# Only the `posixv2` origin publishes; fed.sh refuses other variants.
# Mode is `eventual`; see origin-metadata-tx for `transactional`. Each
# chooses the mode, so fed.sh refuses the two together.

ORIGIN_METADATA=true
ORIGIN_METADATA_MODE=eventual

fed_enable_profile metadata
