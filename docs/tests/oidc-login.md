# OIDC login

Part of [What the tests check](README.md).

Under `-p auth-oidc`, every server lets its users log in through an
identity provider (`Origin.EnableOIDC` and the other modules' flags),
and the registry has one prove who registers a prefix from the command
line. The provider is the `idp` service, an OpenID Connect provider of
the framework's own (`framework/idp/idp.py`), at `https://idp:9007`
(`OIDC.Issuer`), standing in for CILogon: its users, their passwords,
and their groups are `framework/etc/idp-users.json`, and its one client
is the ID and secret that `fed.sh` makes at random for the servers
(`framework/var/generated/oidc-client-{id,secret}`). It serves
discovery, a JWKS, the authorization code flow, the device flow, and
userinfo, signs its ID tokens itself (RS256, with a key it makes as it
starts), and logs every request in `framework/var/data/idp/requests.log`,
one JSON object per line, never a secret. `framework/testlib/oidc.py`
plays the browser.

A server begins a login by sending the user to the provider's
authorization endpoint (`/api/v1.0/auth/oauth/login`; `handleOAuthLogin`
in `web_ui/oauth2_client.go`), naming its client, its callback, and a
state, which it keeps in a session cookie too. The user logs in at the
provider, which sends them back to the callback with a code; the server
checks the state against its cookie, trades the code for tokens at the
provider, asks its userinfo endpoint who the user is, finds or makes its
record of them, and sets its login cookie (`handleOAuthCallback`). The
callback is on `OIDC.ClientRedirectHostname`, `localhost` and the
server's published port, for a browser on the host; the suite follows it
to the server's own host instead. The suite skips without the provider.
A scenario with several cases records a row per case, named `<scenario>
<case>`; the first four ask every server of the shape, by web URL.

- `enabled`: each server's `/api/v1.0/auth/oauth` must list each module
  it runs (`listOIDCEnabledServersHandler`), and its login route answer
  307 to the provider's authorization endpoint, asking for a code
  (`response_type`) for the servers' client, to be sent back to
  `https://localhost:<port>/api/v1.0/auth/oauth/callback`, with a state,
  and set the session cookie.
- `login`: `idp-member` logs in at each server. `whoami` must then name
  them by their `preferred_username`, as no admin (`role` `user`); a
  second login must find the same user; the server's admin must list one
  user of that name, whose record links to the provider's subject and
  issuer; and the provider's log must show the server trading each
  login's code at the token endpoint, as the servers' client, and
  asking userinfo who the user is.
- `callback`: at the issuer origin, a login's callback sent without its
  session cookie must be refused (400), as must one whose state is not
  the cookie's; with the cookie it must log the user in; and sent again,
  its code used, it must fail (the provider refuses the code), and log
  no one in.
- `collision`: `idp-collider`, whose `preferred_username` is `admin`,
  the built-in administrator's, logs in at the issuer origin. The server
  must walk to the next claim of `Server.AutoEnrollUsernameClaims`, the
  email, for the username (`LookupOrBootstrapUser` in
  `database/collection.go`), and give them no admin role, nor the
  admin's routes (403 for `GET /api/v1.0/users`).
- `registry-identity`: `pelican namespace register --with-identity` of
  a prefix of the run's. The registry starts the provider's device flow
  (`cliRegisterNamespace` in `registry/registry.go`) and hands the
  client the verification URL, which the suite approves at the
  provider as `idp-member`; the client polls the registry, which polls
  the provider's token endpoint, and once it has an access token asks
  userinfo who the user is, and registers the prefix with that identity.
  The registration must hold the client's key and `idp-member`'s
  identity (the provider's userinfo: their subject and
  `preferred_username`), and name the registry's user of that identity
  as its owner, whom `login` made. Then `pelican namespace delete` must
  remove it. It skips for a standalone origin, which has no registry.
- The last three restart origin-0 with `Server.UIAdminUsers` naming
  `idp-owner`, and `Issuer.GroupSource` `oidc`, in the tests' own layer
  ([service control](../../README.md#service-control)), and restore it
  afterward. The shape itself keeps `internal`, since under `oidc` a
  password login carries no groups (`GroupsForLogin`), which the
  `issuer`, `collections`, and `accounts` suites need. They need a
  control helper, and skip in the `tiny` topology, whose origin is the
  whole federation's process; `groups` and `client` also need the
  embedded issuer and a protected namespace that takes direct writes,
  which hold their objects, in each group's project prefix
  (`/data/issuer/projects/<group>/`, where the templates grant the
  group), at every origin, removed afterward.
  - `admin`: with origin-0 restarted, `idp-owner` must be its admin by
    `whoami`, and may list its users; so must `idp-admin`, whose claimed
    group `test-server-admins` is in `Server.AdminGroups`
    (`RecordAssertedGroups`); `idp-member` must be no admin, and refused
    the users. Once restored, `idp-owner` must be no admin.
  - `groups`: through a client of origin-0's issuer that the admin
    creates, `idp-member`, logged in through the provider, must be
    granted by the device flow exactly what the templates give
    `test-project-a` (`framework/config.d/base/40-origin.yaml`): read and
    create in its project, and read in the archive; and the token must
    name the group (`wlcg.groups`). `idp-admin`, in `test-project-b`,
    likewise for that group alone; `idp-outsider`, in no group, must be
    granted no data scope, or refused. Then `idp-member`'s token must
    read `test-project-a`'s object straight at origin-0 and by `pelican
    object get` through the federation, and be refused
    `test-project-b`'s.
  - `client`: `pelican object get` of `test-project-a`'s object with no
    token: the client registers itself with the issuer and starts a
    device flow, which the suite approves as `idp-member`, logged in
    through the provider (as the `issuer` suite's `client` does with a
    password login); the client must then get the object.
