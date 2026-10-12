# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Let users log in to every server's web UI through an identity provider
# (Origin.EnableOIDC and the other modules' flags), and the registry ask
# one for a registrant's identity (`pelican namespace register
# --with-identity`): the `idp` service, an OpenID Connect provider of
# the framework's own (framework/idp/idp.py), at https://idp:9007
# (OIDC.Issuer, which fed.sh sets), standing in for CILogon. Its users,
# their passwords, and their groups are in framework/etc/idp-users.json;
# its one client is the ID and secret in
# framework/var/generated/oidc-client-{id,secret}, which fed.sh makes at
# random, and which the servers read. The oidc suite tests the login, and
# what the servers make of the provider's users and groups.
#
# How users log in is independent of how the servers store data, so this
# combines with any other preset. Admin pages refuse a user who logs in
# this way unless Server.UIAdminUsers or Server.AdminGroups names them,
# or an admin has granted them access; the oidc suite restarts origin-0
# to name one, and to take groups from the provider's claims
# (Issuer.GroupSource: oidc), which the shape itself leaves `internal`,
# as the issuer, collections, and accounts suites need.
#
# To log in from a browser, map `idp` to 127.0.0.1 in your hosts file:
# the servers send the browser to https://idp:9007, which Compose
# publishes on that port. To use another provider instead, write a
# preset of your own that sets ENABLE_OIDC without this profile, puts
# its client in local/etc/oidc-client-{id,secret}, and sets OIDC.Issuer
# (or every OIDC.*Endpoint) in local/config.d/base/.

ENABLE_OIDC=true
fed_enable_profile idp
