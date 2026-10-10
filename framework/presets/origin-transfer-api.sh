# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Turn on the transfer API in every origin (Origin.EnableTransferAPI): a
# service that runs transfer jobs, such as third-party copies, on the
# origin's behalf, with storage credentials it keeps for each user, and
# only where a job's source or destination is one of the origin's
# exports. It takes tokens with the pelican.transfer scope from the
# origin's own issuer. The transfer-api suite tests origin-0's.

ORIGIN_TRANSFER_API=true
