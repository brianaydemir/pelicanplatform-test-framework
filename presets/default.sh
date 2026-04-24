# shellcheck shell=sh
# shellcheck disable=SC2034  # read by fed.sh and Compose
#
# The baseline: one director, registry, origin, and cache; no XRootD; no
# external identity provider. fed.sh always sources this first, then any
# -p presets, then re-applies values from the caller's environment.
#
# This is the only place a default is set. The Compose files have no
# fallbacks, so running `docker compose` directly fails loudly.

# `full` (docker-compose.yaml) or `tiny` (docker-compose.tiny.yaml).
TOPOLOGY=full

# Directory names under config.d/origin/ and config.d/cache/.
ORIGIN_VARIANT=posixv2
CACHE_VARIANT=v2

# Origin.EnableIssuer and Origin.EnableOIDC.
ORIGIN_ENABLE_ISSUER=true
ORIGIN_ENABLE_OIDC=false

# Logging.Level for every Pelican service.
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
