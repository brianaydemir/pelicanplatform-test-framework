# shellcheck shell=sh
#
# Add a second director, director-1. Every server advertises to both,
# and each forwards what it learns to the other, so that either can
# answer any request. Both sign as the federation, whose JWKS is
# director-0's, so `./fed.sh keys` keeps their keys in step, as it does
# the origins'.

fed_enable_profile multi-director
