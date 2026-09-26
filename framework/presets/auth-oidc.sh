# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Let users log in to every server's web UI through an external identity
# provider. How users log in is independent of how the servers store
# data, so this combines with any other preset.
#
# framework/etc/oidc-client-{id,secret} are placeholders, so logging in
# does not work until you register a client with the provider and put
# its ID and secret in local/etc/oidc-client-{id,secret}. Register
# https://localhost:<port>/api/v1.0/auth/oauth/callback as a redirect
# URI for each server's port (see the README's Ports).
#
# The provider is CILogon, or GitHub when Issuer.GroupSource is `github`:
# Pelican takes its default endpoints and claims from that setting. For
# any other provider, set OIDC.Issuer and every OIDC.*Endpoint in
# local/config.d/base/. OIDC.Issuer alone is not enough: Pelican fills in
# an endpoint from the provider's discovery document only if it has no
# value, and a default counts as one (config/oidc_metadata.go). Likewise,
# to log in through one provider and take groups from another source,
# set the endpoints explicitly.
#
# Admin pages refuse a user who logs in this way unless
# Server.UIAdminUsers or Server.AdminGroups names them, or an admin has
# granted them access. Nothing else depends on this preset.

ENABLE_OIDC=true
