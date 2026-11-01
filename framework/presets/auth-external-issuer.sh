# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have the origins' exports trust a token issuer that is neither theirs
# nor the federation's: https://discovery:8444/issuer, served by the
# discovery host (framework/etc/nginx-discovery.conf). Its key is in
# framework/var/issuer-keys/issuer/, which `./fed.sh keys` manages like
# the servers'; fed.sh publishes the public keys in
# framework/var/generated/issuer/.
#
# The origins' own issuers are off (Origin.EnableIssuer), so the exports
# trust only that issuer and the federation. The caches check tokens
# against it too. The tests' `server` credential signs with its key and
# names it as the issuer. origin-2 (`topo-multi-owner`) keeps its own
# issuer, as another owner would.

EXTERNAL_ISSUER=true
ORIGIN_ENABLE_ISSUER=false
