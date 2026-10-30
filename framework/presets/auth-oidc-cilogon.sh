# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Let users log in to every server's web UI through CILogon
# (Origin.EnableOIDC and the other modules' flags), at
# https://cilogon.org (OIDC.Issuer, which fed.sh sets), and the registry
# ask it for a registrant's identity (`pelican namespace register
# --with-identity`). The client is your own, registered with CILogon:
# put its ID and secret in local/etc/oidc-client-{id,secret}, which
# fed.sh insists on, and register each server's OAuth callback with it,
# https://localhost:<port>/api/v1.0/auth/oauth/callback, at the port
# that its Server.ClientRedirectHostname names (README.md, Ports).
#
# Like `auth-oidc-local`, this combines with any other preset, but not
# with that one. Admin pages refuse a user who logs in this way unless
# Server.UIAdminUsers or Server.AdminGroups names them, or an admin has
# granted them access; name them in local/config.d/base/. The oidc
# suite tests only the framework's own provider, so it skips here.

ENABLE_OIDC=true
OIDC_CILOGON=true
