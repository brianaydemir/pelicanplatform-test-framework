# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Put the directors behind one name, as a federation that runs them for
# high availability does: `director`, a load balancer (nginx, passing TLS
# through to director-0 and director-1 in turn;
# framework/etc/nginx-director.conf), which the federation's metadata
# names as its director, and which clients and every server's
# Federation.DirectorUrl use. Each director's Server.ExternalWebUrl is
# the shared name, https://director:8444, as the tokens its web API takes
# and its comparison of the federation's metadata expect. Its
# Director.AdvertiseUrl is its own, https://director-N:8444, which the
# metadata lists for the servers to advertise to, and the directors to
# forward ads to; and so is its name (Xrootd.Sitename), or else each
# would take it from its external URL, and the two would pass for one.
# fed.sh writes these in framework/var/generated/instance/. Under
# `director-discovery`, every director serves the federation's metadata,
# since any may answer at the shared name.
#
# Needs `topo-multi-director`, for the second director. fed.sh refuses
# `origin-broker`: an origin polls one director's broker, and a request
# for a brokered connection must reach that same director, which a load
# balancer cannot promise. The suites that ask each director ask
# director-0 and director-1 by name; the clients, and so the federation
# suite's `ready` and the transfers suite, go through the load balancer.

DIRECTOR_HA=true

fed_enable_profile director-ha
