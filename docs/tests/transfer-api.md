# Transfer API

Part of [What the tests check](README.md).

The `transfer-api` suite tests the transfer API, which runs jobs such as
third-party copies with each user's stored credentials (Pelican's
`docs/transfer-server-design.md`). Under `-p origin-transfer-api`,
origin-0 runs it, and must confine jobs to its exports. Under
`-p topo-transfer-server`, a transfer server, `transfer`, runs it, and
confines them to nothing. The suite skips without either, or where
`/protected-a` takes no writes. Scenarios that store no credential run
in the WLCG pass only ([token profiles](README.md#token-profiles)).

- `ping`, `auth-none`, `auth-storage-token`, `auth-unknown-key`: The
  public ping must name the service `transfer`. A request must be
  refused (401 or 403, `UNAUTHORIZED`) with no token, a storage token,
  or a `pelican.transfer` token signed by an unknown key.
- `credentials`: A credential must be listed by its ID, and never show
  its token. A second of the same name must conflict (409).
- `copy`: `pelican object copy --transfer-server ... --wait` must copy an
  object byte for byte (`server`). With `--dest-origin` naming origin-0
  (`dest-origin`), it must land where origin-0 runs the API, and
  elsewhere fail, saying the service is not available.
- `job-status`: A finished job must report `completed`, and be listed.
- `bad-dest-credential`: A copy with a read-only destination credential
  must fail, and write nothing.
- `unknown-credential`: A job naming a missing credential must be
  refused (400, `INVALID_REQUEST`).
- `other-user`: Another user must neither get, list, nor cancel a job
  (404), nor use its owner's credential (400).
- `path-not-allowed`: A job outside the exports must be refused (403,
  `PATH_NOT_ALLOWED`); a transfer server must take it (201) and fail it.
- `cancel`, `cancel-finished`: Cancelling an unknown job must be not
  found (404). A completed job, cancelled, must stay `completed`, and a
  failed one `failed`; the cancel may be taken (2xx) or refused (409).
- `local-get`, `local-put`: A job must neither download to origin-0's
  filesystem outside an export, nor upload from it. A transfer server
  must do both: download to `/var/lib/pelican` (`local-get`), and upload
  `/etc/hostname` (`local-put`).
- `subjects`: Tokens whose subjects are a CILogon URL, one character, 65
  characters, or `admin` must each act as a user of their own, who can
  list jobs and run a copy that the suite's user cannot see (404; not
  checked for `admin`). A row per subject.
- `events`: A job's event stream (`GET /jobs/<id>/events`) must carry
  `status` events, the last `completed`, and then end. Another user's
  stream of it must be not found (404).
- `ssrf-and-prefixes`: A source on link-local or loopback, not in
  `Server.SSRFProtection.AllowedCIDRs`, must be refused (403,
  `ENDPOINT_NOT_ALLOWED`; `endpoints`). A job under `/protected-ab`
  must be refused as outside `/protected-a` (403, `PATH_NOT_ALLOWED`;
  `prefix`); a transfer server must take it and fail it. No token or
  OAuth2 client secret may lie in plain in the host's database
  (`secrets`; checked last).
- `credential-cli`: `pelican transfer credential add`, `list`, and
  `delete` must agree with the API, and never print the token.
- `enabled-groups`, `oauth-clients`: The suite restarts the API's host
  with `Transfer.EnabledGroups` and `Transfer.EnableOAuth2Clients`, and
  afterward without ([service control](../../README.md#service-control)).
  Both need a control helper, and skip under `-p topo-tiny`.
  - `enabled-groups`: The host's issuer must grant `pelican.transfer` to
    a member of the group, and not to another user (`issuer`). The API
    must take the member's token and refuse the other's (`token`), and
    likewise their login cookies (403; `cookie`).
  - `oauth-clients`: Off, the default, the routes must refuse everyone
    (403, `DISABLED`; `disabled`). On, a client must be registered
    (201), listed without its secret, and refused a second time (409;
    `crud`); a token exchange through it must yield a credential that a
    copy uses (`bootstrap`); and once that credential's access token
    expires, a copy must refresh it (`refresh`; needs
    `-p origin-short-tokens`). `bootstrap` and `refresh` skip with the
    origins' issuer off or external.
- `restart`: The API's host restarts during a copy, which a `posixv2`
  origin-0 slows, restarted meanwhile with `Origin.TransferRateLimit`.
  The job must then be `failed`, saying a restart interrupted it
  (`interrupted`). A credential stored before must still work
  (`credential`), and a job finished before, cancelled, keep its status
  (`forgotten`). Needs a control helper, and skips under `-p topo-tiny`.
