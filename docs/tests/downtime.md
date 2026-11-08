# Downtime

Part of [What the tests check](README.md).

The [`federation`](federation.md) suite's `downtime` scenario declares the
last federated cache down for ten minutes through its web API
(`/api/v1.0/downtime`), and with several origins, the last origin too.
Within 150 seconds, no director may name it. Once the scenario ends the
downtime, which it does even if interrupted, every director must name it
again within 150 seconds.

Skips a [standalone origin](../../framework/presets/topo-standalone.sh),
[`-p topo-tiny`](../../framework/presets/topo-tiny.sh), and a
[shape](../glossary.md#shape) with no federated cache and one origin.
