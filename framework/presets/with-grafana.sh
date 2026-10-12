# shellcheck shell=sh
#
# Add Grafana at http://localhost:9003 (admin/admin). No datasource is
# provisioned; add one by hand (it persists in framework/var/grafana):
#
#   Type             Prometheus
#   URL              https://origin-0:8444/api/v1.0/prometheus
#   Skip TLS Verify  on (Grafana does not trust the framework CA)
#   HTTP headers     Authorization: Bearer <token>
#
# The query API takes a token with scope monitoring.query from the
# server's own issuer (Monitoring.PromQLAuthorization, on by default),
# or the admin's login cookie. For a token, run in the dev container, as
# framework/testlib/credentials.py mints the tests' (the key is the .pem
# in issuer-keys/origin-0/ that sorts first):
#
#   cd /scratch/framework/var && bin/linux/pelican token create \
#       --issuer https://origin-0:8444 --subject grafana --scope-path / \
#       --raw-scope monitoring.query --lifetime 86400 \
#       --private-key issuer-keys/origin-0/50-initial.pem \
#       "$(cat generated/federation-url)/"

fed_enable_profile monitoring
