# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# Have /public take writes too: an export that anyone may read and a
# token may write (`Capabilities: ["PublicReads", "Writes", ...]`, the
# example in Pelican's own documentation of Origin.Exports; what
# Origin.EnablePublicReads and Origin.EnableWrites give an origin of one
# export), as an origin of public data that a project writes to does.
# Pelican then gives /public an issuer, like the protected namespaces'
# (handleIssuersIfNeeded in server_utils/origin.go): a token must name
# it to write, while reads still take none. fed.sh adds Writes to
# /public's capabilities (fed_namespace_caps), and the tests read what
# each namespace allows from framework/var/generated/exports.
#
# The suites that write test /public too: the transfers and auth suites
# present every credential for a put there, of which only the origins'
# own key's and Server.IssuerJwks's must be taken; the federation
# suite's redirects and hints check that the directors send a bare-path
# PUT to an origin, and still name /public's issuer; and the commands
# suite's /public rows run as they do, with no token, and must still be
# refused. /public has no keys of its own (no IssuerJwks), so the
# credentials that need a namespace's own key stay /protected-a's and
# /protected-b's. fed.sh refuses `origin-no-direct`, under which Pelican
# allows only reads; `origin-pstore`, whose /public reads /protected-a's
# storage; and a shape that does not export /public (`origin-https`,
# `origin-httpsv2`, and `origin-broker` export /protected-a alone, unless
# `origin-public` picks /public instead).

ORIGIN_PUBLIC_WRITES=true
