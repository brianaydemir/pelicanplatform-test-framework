# shellcheck shell=sh
#
# Add a second director. Origins and caches advertise to both, and the
# two share an issuer key. Clients still go to director-0 (there is no
# load balancer), so this tests dual advertisement, not failover.

fed_enable_profile ha
