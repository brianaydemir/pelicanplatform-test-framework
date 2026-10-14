# Downtime

Part of [What the tests check](README.md).

A server's admin declares it down through its web API
(`/api/v1.0/downtime`), as before maintenance; the server tells the
registry, from which the directors learn of it, and stop sending
clients there. The `federation` suite's `downtime` check declares the
last federated cache down for an hour, as its admin, with a token like
`pelican cache introspect`'s; within 150 seconds no director may name
it for the test object (the directors ask the registry every minute,
`Director.RegistryQueryInterval`); then it ends the downtime, after
which, as soon, every director must name it again. With several
origins, it does the same for the last origin. The check is skipped in
the `tiny` topology, whose servers and director are one process, and
with no cache and one origin (`-p topo-no-cache`), when nothing could
be named instead.
