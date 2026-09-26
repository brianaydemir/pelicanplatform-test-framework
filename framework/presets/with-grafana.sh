# shellcheck shell=sh
#
# Add Grafana at http://localhost:9003 (admin/admin). No datasource is
# provisioned; add one by hand (it persists in framework/var/grafana):
#
#   Type             Prometheus
#   URL              https://origin-0:8444/api/v1.0/prometheus
#   Skip TLS Verify  on (Grafana does not trust the framework CA)
#
# No credentials are needed: config.d/base/20-server.yaml turns off
# Monitoring.PromQLAuthorization.

fed_enable_profile monitoring
