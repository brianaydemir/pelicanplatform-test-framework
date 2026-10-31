# Downtime

Part of [What the tests check](README.md).

The [`federation`](federation.md) suite's `downtime` scenario declares
the last federated cache down for an hour through its web API
(`/api/v1.0/downtime`), and with several origins, the last origin too.
Within 150 seconds, no director may name it. Once the downtime ends,
every director must name it again within 150 seconds.

Skips a standalone origin, `-p topo-tiny`, and a shape with no federated
cache and one origin.
