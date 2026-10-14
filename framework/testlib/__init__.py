"""What test.py, its suites (framework/suites/), and
framework/init-data.py share.

  accounts     a server's web API as one of its users; its users and
               groups; logging in
  ads          advertisements as a server posts them, signed as it
  blocks       sizes and ranges at the stores' block boundaries, checking
               a range, and a cache's store on disk
  cacheapi     a V2 cache's own APIs: prestage, evict, stats, purges, and
               introspection
  collections  collections, through an origin's web API
  common       paths, messages, the command line, and generated/
  control      restarting, stopping, or reconfiguring a service mid-run,
               through framework/control.py on the host
  credentials  the credentials the tests present, and what each allows
  digests      checksums as a Digest header reports them
  director     asking a director where it would send a request, and what
               its service discovery should list
  issuer       the origins' embedded OAuth2 issuer, as a client of it,
               and the prefixes its templates grant
  jwt          tokens of any header and claims, signed any way
  metrics      a server's /metrics, and how a counter moved
  names        object names that a URL or a listing must encode
  oidc         logging in through the identity provider, as a browser
  owners       another owner's namespaces beside the origins', for the
               owners suite
  recorder     what the metadata recorder keeps of the origins' events
  registry     the registry's own APIs: registering a prefix, keys of a
               run's own, and the keys in a JWKS
  report       one row per test case, in framework/var/results/
  session      what every suite shares in one run: tokens, the client,
               and reading what it printed
  stores       what the origins hold, putting objects straight there, and
               what their storage cannot do
  stub         a stub HTTP server in the dev container, for servers to
               fetch from, which answers as a test tells it
  transfers    the transfers suite's scenarios, batches, and verdicts
  web          HTTP(S) requests straight to a server
  webapi       every server's web API, and the admin's and a user's
               credentials for it
  webdav       PROPFIND, and reading the listings it gets; COPY and MOVE

Python 3.9 and its standard library only: that is what AlmaLinux 9 (the
dev container) and macOS ship.
"""
