# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# The baseline: one director, registry, origin, and cache; no XRootD; no
# external identity provider or token issuer. fed.sh always sources this
# first, then any -p presets, then re-applies the caller's environment,
# but only for the knobs at the end, which don't shape the federation.
#
# This and framework/environment.cfg are the only places a default is
# set. The Compose files have no fallbacks, so running `docker compose`
# directly fails loudly.

#---------------------------------------------------------------------------
# The knobs that shape the federation, which only presets set.

# `full` (docker-compose.yaml), `tiny` (docker-compose.tiny.yaml), or
# `standalone` (origin-0 alone, from docker-compose.yaml).
# presets/topo-basic.sh, topo-tiny.sh, and topo-standalone.sh set it.
TOPOLOGY=full

# Directory names under config.d/origin/ and config.d/cache/. Each
# origin-<storage> or cache-<kind> preset sets one of them.
ORIGIN_VARIANT=posixv2
CACHE_VARIANT=v2

# Origin.EnableIssuer.
ORIGIN_ENABLE_ISSUER=true

# Whether the origins' exports trust an external issuer rather than their
# own (see presets/auth-external-issuer.sh).
EXTERNAL_ISSUER=false

# Whether the origins' embedded issuer mints tokens that soon expire
# (Issuer.AccessTokenLifetime, Issuer.RefreshTokenLifetime, and
# Issuer.RefreshTokenGracePeriod; see presets/origin-short-tokens.sh).
ISSUER_SHORT_TOKENS=false

# Whether the /protected-b export has authorization templates of its
# own, in place of the global ones (see
# presets/origin-export-templates.sh).
ORIGIN_EXPORT_TEMPLATES=false

# The namespaces the origins export, of `public`, `protected-a`, and
# `protected-b` (see presets/origin-httpsv2.sh and
# presets/origin-no-direct.sh). /public is read without a token, and
# takes no writes, so it has no issuer. The others take a token for
# either, and each has an issuer and keys of its own; the tests need
# at least one of them.
ORIGIN_NAMESPACES="public protected-a protected-b"

# Origin.DisableDirectClients, which also leaves the exports only reads
# (see presets/origin-no-direct.sh).
ORIGIN_DISABLE_DIRECT_CLIENTS=false

# EnableOIDC for every server: origin, cache, director, and registry.
ENABLE_OIDC=false

# Origin.Posc.Enabled, Origin.Metadata.{Enabled,TrackAccess}, and
# Origin.Metadata.Mode (see presets/origin-posc.sh and
# presets/origin-metadata.sh).
ORIGIN_POSC=false
ORIGIN_METADATA=false
ORIGIN_METADATA_MODE=eventual

# Origin.EnableAtomicUploads, XRootD's POSC (see
# presets/origin-atomic-uploads.sh).
ORIGIN_ATOMIC_UPLOADS=false

# Origin.CacheControl: what the origins send as Cache-Control. Empty
# sends none, and a cache then decides for itself how long an object
# stays fresh (see presets/origin-max-age.sh).
ORIGIN_CACHE_CONTROL=

# Origin.Multiuser, with the tests' tokens mapped to a user (see
# presets/origin-multiuser.sh).
ORIGIN_MULTIUSER=false

# Origin.EnableBroker, with director-0 as the connection broker (see
# presets/origin-broker.sh).
ORIGIN_BROKER=false

# Origin.EnableTransferAPI (see presets/origin-transfer-api.sh).
ORIGIN_TRANSFER_API=false

# Cache.TieringTargets for every V2 cache (see presets/cache-tiered.sh).
CACHE_TIERING=false

# Cache.MemoryCacheSize for every V2 cache, as Pelican reads it, e.g. 64MB
# (see presets/cache-memory.sh). Empty: no memory cache.
CACHE_MEMORY_SIZE=

# Cache.HighWaterMark and Cache.LowWaterMark for every V2 cache, in MiB
# (see presets/cache-small.sh). Empty: Pelican's defaults, a share of the
# whole disk.
CACHE_HIGH_WATER_MIB=
CACHE_LOW_WATER_MIB=

# Server.DropPrivileges for every Pelican server (see
# presets/server-unprivileged.sh).
SERVER_DROP_PRIVILEGES=false

# Director.DefaultResponse: where a director sends a GET of a bare path,
# e.g. https://director-0:8444/public/data/0.0, which the Pelican client
# never asks for but curl and browsers do: `cache` or `origin` (see
# presets/director-default-origin.sh).
DIRECTOR_DEFAULT_RESPONSE=cache

# Registry.RequireOriginApproval and Registry.RequireCacheApproval: whether
# a registration serves nothing until an administrator approves it (see
# presets/registry-approval.sh).
REGISTRY_REQUIRE_APPROVAL=false

#---------------------------------------------------------------------------
# The knobs that don't shape it, which the caller's environment may also
# set.

# Logging.Level for every Pelican service, and the client in dev.
PELICAN_LOG_LEVEL=info

# Different on purpose, to catch timestamps rendered in the wrong zone.
TZ_SERVICES=UTC
TZ_STORAGE=America/Chicago

# Memory limits. FED_MEM is the `tiny` topology's single container.
ORIGIN_MEM=512M
CACHE_MEM=512M
DIRECTOR_MEM=256M
REGISTRY_MEM=128M
DEV_MEM=1024M
FED_MEM=1024M
