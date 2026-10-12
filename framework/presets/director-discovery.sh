# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have director-0 serve the federation's metadata
# (Director.EnableFederationMetadataHosting), as the discovery host that
# clients and servers find the federation through, in place of the
# `discovery` service, which then does not run: the federation is
# pelican://director-0:8444, as a federation whose discovery URL is its
# director's is. The director builds the document from its own
# configuration (director/discovery.go in Pelican), so what it names
# must agree with fed.sh's own document
# (framework/var/generated/discovery.json), which the federation suite's
# `discovery` scenario checks; and it serves the federation's OpenID
# configuration, by which the origins find the keys that sign federation
# tokens. Pelican's director offers its connection broker by default
# (Director.EnableBroker), so the document names one at director-0,
# which the caches then use to reach it, as they would in such a
# federation.
#
# fed.sh refuses `topo-tiny`, whose director is its discovery host
# already, and `topo-standalone`, which has no director. Under
# `auth-external-issuer`, the `discovery` service still runs, to serve
# the external issuer, but nothing looks for the federation there.

DIRECTOR_DISCOVERY=true
