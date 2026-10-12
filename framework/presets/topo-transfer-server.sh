# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Add a transfer server, `transfer`: the transfer API in a process of its
# own (`pelican-server serve --module transfer`; transfer/transfer.go in
# Pelican), beside the origins rather than embedded in one
# (`origin-transfer-api`). It keeps each user's storage credentials and
# runs their jobs, as the embedded API does, but confines a job to no
# export: whatever it can reach, it moves. Its users authenticate
# through a local issuer of its own, which mints pelican.transfer
# tokens at https://transfer:8444/api/v1.0/issuer/ns/pelican/local-issuer,
# naming https://transfer:8444 as their issuer, and signs them with the
# server's key (framework/var/issuer-keys/transfer/). The transfer-api
# suite tests it in place of origin-0's, with that key, so fed.sh refuses
# `origin-transfer-api` beside it; and the full topology alone has room
# for the service.
#
# Pelican's main has the module, but no release yet: a released
# pelican-server refuses `--module transfer`, so build IMAGE_ORIGIN from
# main (see the README's Server images), from which the service runs.
# Until a release has it, smoke.sh runs no shape with this preset.

TRANSFER_SERVER=true

fed_enable_profile transfer
