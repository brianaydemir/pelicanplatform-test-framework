# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Run the origins without an issuer (Origin.EnableIssuer), as Pelican
# does by default, and as an origin whose tokens are minted elsewhere
# runs: the origins mint no tokens, and every protected namespace's
# issuer is the origin itself, https://origin-0:8444 (its web URL;
# https://fed:8444/api/v1.0/origin under `topo-tiny`, where a director
# shares its process), whose keys are the origin's own
# (framework/var/issuer-keys/origin-0/, which `./fed.sh keys` manages).
# A token is signed with that key out of band, naming that issuer, as
# `pelican token create --private-key` does, and as the tests mint
# their credentials in every shape. origin-2 (`topo-multi-owner`) keeps
# its own issuer, as another owner would.
#
# With no issuer, nothing grants users tokens: the collections, issuer,
# and accounts suites skip, as do the parts of other suites that mint
# from it, and the auth and transfers suites skip the credentials that
# tell one namespace's issuer or keys from another's, since every
# namespace has the origin's. The rest run as usual. fed.sh refuses
# `origin-short-tokens` and `origin-export-templates`, which configure
# the issuer, and `auth-external-issuer`, which turns it off too, in
# favor of another.

ORIGIN_ENABLE_ISSUER=false
