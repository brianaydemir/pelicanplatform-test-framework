# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have director-0 serve the federation's metadata
# (Director.EnableFederationMetadataHosting) in place of the `discovery`
# service, which then does not run: the federation is
# pelican://director-0:8444, as a federation whose discovery URL is its
# director's is. The director builds the document from its own
# configuration (director/discovery.go in Pelican), so what it names must
# agree with fed.sh's (framework/var/generated/discovery.json), which the
# federation suite's `discovery` scenario checks. It also serves the
# federation's OpenID configuration, by which the origins find the keys
# that sign federation tokens. The director offers its connection broker
# by default (Director.EnableBroker), so the document names one at
# director-0, which the caches then use, as they would in such a
# federation. Under `director-ha`, every director serves the metadata,
# and the federation and its broker are the load balancer's:
# pelican://director:8444.
#
# fed.sh refuses `topo-tiny`, whose director is its discovery host
# already, and `topo-standalone`, which has no director. Under
# `auth-external-issuer`, the `discovery` service still runs, to serve
# the external issuer, but nothing looks for the federation there.

DIRECTOR_DISCOVERY=true
