# Transfer API

Part of [What the tests check](README.md).

Under `-p origin-transfer-api`, origin-0 runs the transfer API
(`Origin.EnableTransferAPI`; `transfer/` in Pelican), a service that
runs transfer jobs on the server, such as third-party copies, with
storage credentials it keeps for each user, and only where a job's
source or destination is one of the origin's exports. Under
`-p topo-transfer-server`, a transfer server of its own, `transfer`,
runs it instead (`pelican-server serve --module transfer`), which
confines a job to no export, and authenticates its users through a
local issuer of its own (`RegisterTransferAPI` and `RegisterLocalIssuer`
in `transfer/transfer.go`). The API takes tokens with the
`pelican.transfer` scope from its host's issuer, which the
`transfer-api` suite mints with that host's key, origin-0's or the
transfer server's, for a subject of the tests' own; its storage
credentials are the `server` tokens for `/protected-a`. The suite's
objects go under `/protected-a/data/xfer/<run>/`, which it removes
afterward, with the credentials it added. A scenario with several
cases records a row per case, named `<scenario> <case>`.

- `ping`, `auth-none`, `auth-storage-token`, and `auth-unknown-key`:
  the public ping says that the service is the transfer service, and a
  request with no token, with a storage token (which lacks
  `pelican.transfer`), or with a `pelican.transfer` token signed by a
  key no server knows, is refused (401 or 403, `UNAUTHORIZED`).
- `credentials`: a credential is listed by its ID, no answer shows its
  token, and a second one of the same name is a conflict (409).
- `copy`, `job-status`, `bad-dest-credential`, `unknown-credential`,
  `other-user`, `path-not-allowed`, `cancel`, `local-get`, and
  `local-put`: `pelican object copy --transfer-server ... --wait` copies
  an object byte for byte (`copy server`), and with `--dest-origin`
  naming origin-0 instead, the client asks the origin whether it runs
  the transfer API (`pingTransferService` in `cmd/object_copy.go`), and
  copies through it where it does, or must fail, saying that the
  service is not available there, and land nothing, where a transfer
  server's is the API (`copy dest-origin`); a finished job reports `completed`, and is
  listed; a copy whose destination credential may only read fails, and
  writes nothing; a job naming a credential that does not exist is
  refused (400); another user can neither see a job nor use a
  credential; a job outside the exports is refused (403,
  `PATH_NOT_ALLOWED`); cancelling an unknown job is not found, and a
  finished one must stay as it finished; and a job may neither download
  to the origin's own filesystem outside an export nor upload from it,
  which Pelican's main does not confine, so those two fail there. A
  transfer server confines a job to nothing, by design: there, a job
  outside the exports must be taken (201), and then fail, since nothing
  serves what it names (`path-not-allowed`); and a job must download to
  the server's own filesystem (`/var/lib/pelican`, its state, where the
  suite then finds the source object, byte for byte; `local-get`) and
  upload from it (`/etc/hostname`, which must land as the file,
  `transfer` and a newline; `local-put`), so that what a user of such a
  server may reach is on record. For a job that finished and is still
  in the manager's memory, Pelican's `CancelJob` answers "job already
  completed", which the handler turns into 500
  (`client_agent/transfer_manager.go`, `transfer/handlers.go`), so
  `cancel` fails, on v26.0.0-rc.3 and main alike.
- `subjects`: `pelican.transfer` tokens whose subjects are a CILogon
  URL, one character, 65 characters, or `admin`, the built-in user's
  name: each must list its jobs (200), run a copy of its own, and see
  no other subject's job. The API resolves each token's issuer and
  subject to a user record of its own, creating one with the subject as
  its username (`GetOrCreateUser`, from `TransferAuthMiddleware` in
  `transfer/auth.go`), and a username must be 2 to 64 characters of a
  narrow set (`ValidateIdentifier` in `database/identifiers.go`), so
  the first three fail with 500, "Failed to resolve user identity".
  The `admin` case shows whether a token for an existing
  username, from the local issuer, is the existing user's or a
  collision.
- `cancel-finished`: a copy whose destination credential may only
  read, which fails, cancelled: the cancel is taken (2xx) or refused as
  too late (409), and the job must stay `failed`. Pelican counts every
  transfer of a failed job as cancelled, and records the job cancelled,
  so the row fails, on v26.0.0-rc.3 and main alike.
- `events`: a job's event stream (`GET /jobs/<id>/events` with `Accept:
  text/event-stream`) must carry `status` events whose data names the
  job, the last of them its terminal status, `completed`, after which
  the stream ends (`StreamJobEvents` in `client_agent/job_events.go`),
  and the job's status must agree; another user's stream of the job is
  not found (404). `pelican object copy --wait` falls back to polling
  when a stream fails, so `copy` would not notice a broken one.
- `ssrf-and-prefixes`: a job whose source is on link-local
  (`169.254.169.254`) or loopback, which the allowed ranges of
  `Server.SSRFProtection.AllowedCIDRs` do not cover, must be refused
  (403, `ENDPOINT_NOT_ALLOWED`); one whose source and destination are
  under `/protected-ab`, which merely begins like an export, 403
  `PATH_NOT_ALLOWED`, since a prefix matches whole path segments (at a
  transfer server, taken and then failed, as `path-not-allowed`); and
  no credential's token, nor the client secret that `oauth-clients`
  registers, may lie in the API's host's database
  (`framework/var/state/`) in plain, which keeps each encrypted
  (`transfer/encryption.go`).
- `credential-cli`: `pelican transfer credential add`, `list`, and
  `delete`, with the user's token in a file, against the API: each
  must agree with it, and neither `add` nor `list` may print the
  token.
- `enabled-groups` and `oauth-clients` need configuration that no shape
  has: `Transfer.EnabledGroups`, naming a group of the run's own
  (`xfer-<run>`), and `Transfer.EnableOAuth2Clients`. The suite restarts
  the API's host once with both, as the last of its configuration
  layers ([service control](../../README.md#service-control)), runs them,
  and restarts it without, waiting each time until the API answers and
  every director names origin-0 again. The restart needs a control
  helper and a topology other than `tiny`, whose origin is the whole
  federation's process; elsewhere both skip, and say why.
  - `enabled-groups`: with the group named, the host's local issuer
    grants `pelican.transfer` to a member who asks for it through the
    device flow, approved with their login cookie, which carries their
    groups, and not to another user (`transferAccessAllowed` in
    `oauth2/issuer/handlers.go`; `issuer`). The member's token is taken
    at the API, and the other's refused (`token`). The member's login
    cookie must be taken too, since the API takes a cookie whose groups
    match (`TransferAuthMiddleware`), and the other's refused (403;
    `cookie`): the cookie carries no `pelican.transfer`, which the
    middleware asks of every token, so the member's is refused too,
    and the row fails, on v26.0.0-rc.3 and main alike.
  - `oauth-clients`: with the clients off, as by default, the routes
    refuse everyone (403, `DISABLED`; `disabled`). With them on, a
    client of the namespace issuer's, which origin-0's admin creates
    for the token exchange and the refresh grant (made-up credentials,
    with the issuer off or external, since the origin can make no
    client of it), is
    registered with the API (`POST /oauth-clients`, 201), listed
    without its secret, refused a second time (409), and deleted
    afterward (`crud`); a token the
    admin got from the issuer is exchanged, through it, for a
    credential (`POST /credentials/bootstrap/token-exchange`, 201;
    `handleTokenExchangeBootstrap` in `transfer/bootstrap.go`), which a
    copy then uses (`bootstrap`); and under `-p origin-short-tokens`,
    once the credential's access token has expired, a copy must still
    complete, since the server refreshes the token first
    (`getDecryptedAccessToken` in `transfer/credentials.go`), and the
    credential's `token_expiry` must have moved on (`refresh`;
    elsewhere it skips, since the tokens last an hour).
- `restart`: the API's host restarted while a copy runs: the job must
  then be `failed`, saying it was interrupted by a restart
  (`reconcileInterruptedJobs` in `transfer/recovery.go`), or, if the
  copy finished first, the row is inconclusive (`interrupted`). A
  `posixv2` origin-0 is first restarted slowed
  (`Origin.TransferRateLimit` 256 KiB a second, which it alone honors),
  so that the copy of the 8 MiB source takes about a minute; afterward
  it is restored. A
  credential stored before the restart must still work (`credential`).
  And a job that finished before the restart, which the manager no
  longer holds, cancelled, must keep its status (`forgotten`): Pelican
  records it cancelled instead, so the row fails, on v26.0.0-rc.3
  and main alike. It needs a control helper and a topology other than
  `tiny`, and skips otherwise.
