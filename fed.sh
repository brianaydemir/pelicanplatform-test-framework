#!/bin/sh
#
# Run a Pelican test federation: choose a shape with presets, then hand
# off to Docker Compose.
#
#   ./fed.sh init                 prepare certificates, binaries, keys, data
#   ./fed.sh up                   start the federation
#   ./fed.sh -p origin-pstore -p topo-multi-cache up
#                                 start a different shape (remembered)
#   ./fed.sh -p default up        ... forgetting the remembered shape
#   ./fed.sh down                 stop everything
#   ./fed.sh restart [SERVICE]... recreate containers to reread configuration
#   ./fed.sh keys                 list issuer keys (also: init, add, rotate)
#   ./fed.sh dev [CMD]...         run a shell (or CMD) in the dev container
#   ./fed.sh test [ARG]...        run ./test.py in the dev container, then
#                                 keep its output and the services' logs
#                                 in framework/var/logs/
#   ./fed.sh control              let a test.py run by hand control services
#   ./fed.sh status               show containers, presets, and images
#   ./fed.sh check                exit 0 if fed.sh accepts the presets
#   ./fed.sh logs -f origin-0     anything else goes to docker compose
#
# Run `init` with the same presets as `up`: the test data depends on them.
# Your customizations go in local/. Everything fed.sh creates goes in
# framework/var/, which ./reset.sh discards.

set -eu

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }
warn()      { printf '%s: warning: %s\n' "${progname}" "$*" >&2; }

# The usage text is the header comment, up to the first blank line.
usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
  printf '\n'
  {
    printf 'Available presets:'
    for preset in framework/presets/*.sh local/presets/*.sh; do
      [ -e "${preset}" ] || continue
      preset=${preset##*/}
      preset=${preset%.sh}
      [ "${preset}" != default ] || continue
      printf ' %s' "${preset}"
    done
    printf '\n'
  } | fold -s -w 76 | sed 's/ *$//'
}

#---------------------------------------------------------------------------
# Every Pelican server the framework can start (`fed` is the `tiny`
# topology). Each owns framework/var/issuer-keys/<svc> and
# framework/var/state/<svc>.
#
# The members of a group share a key. The `origins` export the same
# prefixes, and the registry binds one keyset per prefix; origin-2
# (`topo-multi-owner`), another owner, has prefixes and a key of its own.
# The `directors` both sign as the federation, whose one JWKS is
# director-0's.

fed_services="director-0 director-1 registry origin-0 origin-1 origin-2 cache-0 cache-1 cache-2"
fed_services="${fed_services} transfer fed"
fed_groups="origins directors"
fed_group_origins="origin-0 origin-1"
fed_group_directors="director-0 director-1"

# Key holders that are not Pelican servers: `issuer` is the external token
# issuer that the discovery host serves (`auth-external-issuer`).
fed_key_others="issuer"

fed_certs="framework/var/certs/ca.crt framework/var/certs/tls.crt framework/var/certs/tls.key"

# The Pelican servers' stores, under framework/var/data/.
fed_stores="origin/0 origin/1 origin/2 cache/0 cache/1 cache/2 cache/fed"

#---------------------------------------------------------------------------
# Compose profiles. Presets call fed_enable_profile with one of these,
# each of which a Compose file defines:
#
#   multi-origin      origin-1, a replica of origin-0
#   multi-cache       cache-1
#   multi-owner       origin-2, another owner
#   multi-director    director-1
#   site-local-cache  cache-2, a site-local cache
#   transfer          transfer, a transfer server of its own
#   director-ha       director, a load balancer in front of the directors
#   lab               lab-server, the SSH host
#   idp               idp, the identity provider (`auth-oidc-local`)
#   metadata          metadata and metadata-verifier, the metadata receiver
#   monitoring        grafana
#   webdav            webdav, the backend of https and httpsv2 origins
#   s3                s3, the backend of s3 and s3v2 origins
#
# fed.sh enables four more itself: `federation` (director-0 and the
# registry) in the `full` topology; `cache` (cache-0) with it, unless
# the shape runs no cache (`topo-no-cache`); `discovery` (the discovery
# host) there too, unless director-0 serves the federation's metadata
# (`director-discovery`); and `external-issuer` (the discovery host) for
# `auth-external-issuer`.

fed_profiles="multi-origin multi-cache multi-owner multi-director site-local-cache transfer"
fed_profiles="${fed_profiles} director-ha lab idp metadata monitoring webdav s3"

# True if Compose profile $1 is enabled.
fed_has_profile() {
  case ",${COMPOSE_PROFILES}," in
    *",$1,"*) return 0 ;;
  esac
  return 1
}

# Enable Compose profile $1, unless it is enabled.
fed_add_profile() {
  if ! fed_has_profile "$1"; then
    COMPOSE_PROFILES="${COMPOSE_PROFILES:+${COMPOSE_PROFILES},}$1"
  fi
}

# Enable profile $1 for a preset. It must be in fed_profiles: a profile
# that no Compose file defines would start nothing, silently.
fed_enable_profile() {
  case " ${fed_profiles} " in
    *" $1 "*) ;;
    *) die "no such Compose profile: $1 (try: ${fed_profiles})" ;;
  esac
  fed_add_profile "$1"
}

#---------------------------------------------------------------------------
# Command line. Everything after the first non-option is for Compose.

# Ignore these if the caller's shell has them: the presets decide the
# profiles, TOPOLOGY decides the file, and the project is `pelican`,
# unless local/environment.cfg names another (as smoke.sh does in each
# of its copies of the checkout).
COMPOSE_PROFILES=""
unset COMPOSE_PROJECT_NAME

presets=""
while [ $# -gt 0 ]; do
  case "$1" in
    -p)  [ $# -ge 2 ] || die_usage "-p needs a preset name"
         presets="${presets}${presets:+ }$2"; shift 2 ;;
    -p*) presets="${presets}${presets:+ }${1#-p}"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[ $# -ge 1 ] || { usage >&2; exit 2; }

# Set fed_preset to the file for preset $1, which may be in local/presets/
# or framework/presets/ but not both.
fed_find_preset() {
  fed_preset=""
  if [ -f "local/presets/$1.sh" ] && [ -f "framework/presets/$1.sh" ]; then
    die_usage "preset '$1' is in both local/presets/ and framework/presets/;" \
              "rename yours"
  elif [ -f "local/presets/$1.sh" ]; then
    fed_preset="local/presets/$1.sh"
  elif [ -f "framework/presets/$1.sh" ]; then
    fed_preset="framework/presets/$1.sh"
  else
    die_usage "no such preset: $1"
  fi
}

# framework/var/ holds keys, tokens, and stores, some of which must be
# open to any user for the containers' sake (see fed_prepare_dirs and
# fed_prepare_unprivileged). Keep the host's other accounts out of all of
# it: the containers reach what is in it through their bind mounts, and
# the dev container, which has the checkout, runs as root.
mkdir -p framework/var/generated/conf
chmod 0700 framework/var
presets_given=""
if [ -n "${presets}" ]; then
  presets_given=yes
elif [ -f framework/var/generated/presets ]; then
  presets=$(cat framework/var/generated/presets)
fi

#---------------------------------------------------------------------------
# Settings, lowest precedence first: framework/environment.cfg,
# local/environment.cfg, framework/presets/default.sh, -p presets, then
# the caller's environment.
#
# Only the knobs below are taken from the caller's environment. The
# knobs that shape the federation are not: only presets set them, and
# the caller's values are ignored.

fed_knobs="IMAGE_HUB PELICAN_TAG PELICAN_DEV_TAG PELICAN_SRC_DIR
           PELICAN_LOG_LEVEL TZ_SERVICES TZ_STORAGE
           ORIGIN_MEM CACHE_MEM DIRECTOR_MEM REGISTRY_MEM TRANSFER_MEM DEV_MEM FED_MEM
           IMAGE_ORIGIN IMAGE_CACHE IMAGE_DIRECTOR IMAGE_REGISTRY IMAGE_DEV"

# Knobs that choose one of several things, which no two presets may both
# set: otherwise `-p origin-pstore -p origin-ssh` would quietly be
# `origin-ssh`. While a preset is sourced, each is `fed-unchosen`, so
# that setting one to its default (`-p origin-posixv2`) counts too; a
# preset that reads one sees that.
fed_exclusive_knobs="TOPOLOGY ORIGIN_VARIANT CACHE_VARIANT ORIGIN_METADATA_MODE"
fed_exclusive_knobs="${fed_exclusive_knobs} ORIGIN_ENABLE_ISSUER"

# Save the caller's knobs, one variable each, so that a path with spaces
# survives.
for fed_knob in ${fed_knobs}; do
  eval "fed_caller_${fed_knob}=\${${fed_knob}:-}"
done

# Presets are sourced into this shell, so the loops below name their
# variables fed_*, which no preset sets.
set -a
# shellcheck disable=SC1091
. ./framework/environment.cfg
if [ -f ./local/environment.cfg ]; then
  # shellcheck disable=SC1091
  . ./local/environment.cfg
fi
# shellcheck disable=SC1091
. ./framework/presets/default.sh
for fed_preset_name in ${presets}; do
  fed_find_preset "${fed_preset_name}"
  for fed_knob in ${fed_exclusive_knobs}; do
    eval "fed_was_${fed_knob}=\${${fed_knob}} ${fed_knob}=fed-unchosen"
  done
  # shellcheck disable=SC1090
  . "./${fed_preset}"
  for fed_knob in ${fed_exclusive_knobs}; do
    eval "fed_now=\${${fed_knob}} fed_by=\${fed_chosen_by_${fed_knob}:-}"
    # shellcheck disable=SC2154  # both assigned by the eval above
    if [ "${fed_now}" = fed-unchosen ]; then
      eval "${fed_knob}=\${fed_was_${fed_knob}}"
      continue
    fi
    # `default` sets every knob, so it forgets who chose each.
    if [ "${fed_preset_name}" = default ]; then
      eval "fed_chosen_by_${fed_knob}="
      continue
    fi
    [ -z "${fed_by}" ] || [ "${fed_by}" = "${fed_preset_name}" ] \
        || die_usage "presets '${fed_by}' and '${fed_preset_name}' both set" \
                     "${fed_knob}; choose one"
    eval "fed_chosen_by_${fed_knob}=\${fed_preset_name}"
  done
done
for fed_knob in ${fed_knobs}; do
  eval "[ -n \"\${fed_caller_${fed_knob}}\" ]" || continue
  eval "${fed_knob}=\${fed_caller_${fed_knob}}"
done
set +a
export COMPOSE_PROFILES

# The origins export only /public (`origin-public`), whatever else chose
# the namespaces: the presets that export one namespace pick
# /protected-a, and this picks /public instead.
if [ "${ORIGIN_PUBLIC_ONLY}" = true ]; then
  ORIGIN_NAMESPACES=public
fi

# cache-1's kind (`cache-mixed`): CACHE_1_VARIANT, or the other caches'.
# Compose reads it for cache-1's role layer.
fed_cache_mixed=false
if [ -n "${CACHE_1_VARIANT}" ]; then
  fed_cache_mixed=true
fi
CACHE_1_VARIANT=${CACHE_1_VARIANT:-${CACHE_VARIANT}}
export CACHE_1_VARIANT

# Naming the file also keeps Compose from picking up an override file.
# `standalone` is docker-compose.yaml without the `federation` profile.
case "${TOPOLOGY}" in
  full|standalone) COMPOSE_FILE=framework/docker-compose.yaml ;;
  tiny)            COMPOSE_FILE=framework/docker-compose.tiny.yaml ;;
  *)               die "no such topology: ${TOPOLOGY} (try: full, tiny, standalone)" ;;
esac
fed_topology=${TOPOLOGY}
# The project directory is framework/, where the file is.
export COMPOSE_FILE

# Origin.EnableStandaloneMode, which Compose sets from the topology.
ORIGIN_STANDALONE=false
if [ "${fed_topology}" = standalone ]; then
  ORIGIN_STANDALONE=true
fi
export ORIGIN_STANDALONE

# For docker-compose.yaml. ORIGIN_0_NETWORK is origin-0's network: the
# shared one, or, behind a firewall (`origin-firewalled`), one the caches
# are not on. The caches resolve CACHES_BLACKHOLE to their own loopback:
# origin-0 behind a firewall, so that connections to it are refused, as
# by a firewall, rather than its name unknown; otherwise a name that
# nothing asks for.
ORIGIN_0_NETWORK=default
CACHES_BLACKHOLE=firewalled.invalid
if [ "${ORIGIN_FIREWALLED}" = true ]; then
  ORIGIN_0_NETWORK=firewalled
  CACHES_BLACKHOLE=origin-0
fi
export ORIGIN_0_NETWORK CACHES_BLACKHOLE

# Each image not named otherwise is the published one for its tag.
: "${IMAGE_DEV:=${IMAGE_HUB}/pelican-dev:${PELICAN_DEV_TAG}}"
: "${IMAGE_ORIGIN:=${IMAGE_HUB}/origin:${PELICAN_TAG}}"
: "${IMAGE_CACHE:=${IMAGE_HUB}/cache:${PELICAN_TAG}}"
: "${IMAGE_DIRECTOR:=${IMAGE_HUB}/director:${PELICAN_TAG}}"
: "${IMAGE_REGISTRY:=${IMAGE_HUB}/registry:${PELICAN_TAG}}"
export IMAGE_DEV IMAGE_ORIGIN IMAGE_CACHE IMAGE_DIRECTOR IMAGE_REGISTRY

# Each published port, as PORT_<port>: the port that README.md names,
# PORT_OFFSET above it, on PUBLISH_ADDRESS. Docker takes only an IP
# address there, and an IPv6 one only in brackets.
case "${PORT_OFFSET}" in
  ''|*[!0-9]*) die "PORT_OFFSET must be a number, not '${PORT_OFFSET}'" ;;
esac
case "${PUBLISH_ADDRESS}" in
  ''|*[!0-9.]*)
    case "${PUBLISH_ADDRESS}" in
      \[*:*\]) ;;
      *) die "PUBLISH_ADDRESS must be an IPv4 address, or an IPv6 one in" \
             "brackets, not '${PUBLISH_ADDRESS}'" ;;
    esac ;;
esac
for fed_port in 8444 8445 8446 8447 8448 8449 9000 9001 9002 9003 9004 9005 9006 9007 9008; do
  eval "PORT_${fed_port}=\${PUBLISH_ADDRESS}:\$(( fed_port + PORT_OFFSET ))"
  export "PORT_${fed_port}"
done

#---------------------------------------------------------------------------
# Refuse combinations that would start "fine" and then misbehave. Presets
# of your own can set the knobs too, so the checks are on the knobs, and
# the messages name the presets that set them.

for fed_knob in ORIGIN_ENABLE_ISSUER ENABLE_OIDC EXTERNAL_ISSUER \
    ORIGIN_DISABLE_DIRECT_CLIENTS ORIGIN_POSC ORIGIN_ATOMIC_UPLOADS ORIGIN_METADATA \
    ORIGIN_MULTIUSER ORIGIN_BROKER ORIGIN_TRANSFER_API TRANSFER_SERVER CACHE_TIERING \
    FEDERATED_CACHE SERVER_DROP_PRIVILEGES REGISTRY_REQUIRE_APPROVAL ISSUER_SHORT_TOKENS \
    ORIGIN_EXPORT_TEMPLATES DIRECTOR_DISCOVERY ORIGIN_PUBLIC_ONLY DIRECTOR_HA \
    ORIGIN_SSH_TUNNEL ORIGIN_PUBLIC_WRITES ORIGIN_FIREWALLED OIDC_CILOGON; do
  eval "fed_value=\${${fed_knob}}"
  # shellcheck disable=SC2154  # assigned by the eval above
  case "${fed_value}" in
    true|false) ;;
    *) die "${fed_knob} must be 'true' or 'false', not '${fed_value}'" ;;
  esac
done

# Pelican has only these two.
case "${DIRECTOR_DEFAULT_RESPONSE}" in
  cache|origin) ;;
  *) die "DIRECTOR_DEFAULT_RESPONSE must be 'cache' or 'origin', not" \
         "'${DIRECTOR_DEFAULT_RESPONSE}'" ;;
esac

# Docker would create a missing directory, leaving the role layer empty.
[ -d "framework/config.d/origin/${ORIGIN_VARIANT}" ] \
    || die "no such origin variant: ${ORIGIN_VARIANT}"
[ -d "framework/config.d/cache/${CACHE_VARIANT}" ] \
    || die "no such cache variant: ${CACHE_VARIANT}"
[ -d "framework/config.d/cache/${CACHE_1_VARIANT}" ] \
    || die "no such cache variant: ${CACHE_1_VARIANT} (CACHE_1_VARIANT)"

# Die unless every cache is V2, as knob $1 needs: only a V2 cache reads
# it, or reads it rightly.
fed_require_v2_caches() {
  [ "${CACHE_VARIANT}" = v2 ] && [ "${CACHE_1_VARIANT}" = v2 ] \
      || die "$1 needs every cache V2, but CACHE_VARIANT ('cache-xrootd') is" \
             "'${CACHE_VARIANT}', and CACHE_1_VARIANT ('cache-mixed') '${CACHE_1_VARIANT}'"
}

# fed_generate writes Origin.Exports for every variant. A role layer's
# would lose to it anyway.
if grep -qs '^  Exports:' "framework/config.d/origin/${ORIGIN_VARIANT}"/*.yaml; then
  die "framework/config.d/origin/${ORIGIN_VARIANT}/ sets Origin.Exports, which" \
      "fed.sh generates; teach fed_generate about the variant instead"
fi

# The namespaces the origins export, each once: fed_namespaces. The tests
# probe the federation through the first protected one, or else /public
# (probe_namespace() in framework/testlib/credentials.py).
fed_namespaces=""
for fed_ns in ${ORIGIN_NAMESPACES}; do
  case "${fed_ns}" in
    public|protected-a|protected-b) ;;
    *) die "ORIGIN_NAMESPACES may list 'public', 'protected-a', and 'protected-b'," \
           "not '${fed_ns}'" ;;
  esac
  case " ${fed_namespaces} " in
    *" ${fed_ns} "*) die "ORIGIN_NAMESPACES lists '${fed_ns}' twice" ;;
  esac
  fed_namespaces="${fed_namespaces}${fed_namespaces:+ }${fed_ns}"
done
[ -n "${fed_namespaces}" ] \
    || die "ORIGIN_NAMESPACES is empty; list 'public', 'protected-a', or 'protected-b'"

# How many namespaces the origins export.
fed_count_namespaces() {
  # shellcheck disable=SC2086  # one namespace per word
  set -- ${fed_namespaces}
  printf '%s\n' "$#"
}

# True if the origins export namespace $1.
fed_exports_namespace() {
  case " ${fed_namespaces} " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# Set fed_caps to the capabilities the origins give namespace $1. /public
# takes no token to read, and no writes, and so has no issuer, unless
# ORIGIN_PUBLIC_WRITES (`origin-public-writes`) gives it writes, and so
# an issuer. Without direct clients, Pelican allows only reads.
fed_namespace_caps() {
  case "$1" in
    public)
      fed_caps=PublicReads
      if [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = false ]; then
        if [ "${ORIGIN_PUBLIC_WRITES}" = true ]; then
          fed_caps="${fed_caps} Writes"
        fi
        fed_caps="${fed_caps} Listings DirectReads"
      fi ;;
    protected-*)
      fed_caps=Reads
      if [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = false ]; then
        fed_caps="${fed_caps} Writes Listings DirectReads"
      fi ;;
  esac
}

# Pelican has only these two modes.
case "${ORIGIN_METADATA_MODE}" in
  eventual|transactional) ;;
  *) die "ORIGIN_METADATA_MODE must be 'eventual' or 'transactional'," \
         "not '${ORIGIN_METADATA_MODE}'" ;;
esac

# fed_generate writes it into YAML unquoted.
case "${PELICAN_LOG_LEVEL}" in
  ""|*[!A-Za-z]*)
      die "PELICAN_LOG_LEVEL must be a level such as 'debug', not '${PELICAN_LOG_LEVEL}'" ;;
esac

# The presets that enable profile $1, for the messages below.
fed_profile_presets() {
  case "$1" in
    multi-origin)     echo "'topo-multi-origin'" ;;
    multi-cache)      echo "'topo-multi-cache'" ;;
    multi-owner)      echo "'topo-multi-owner'" ;;
    multi-director)   echo "'topo-multi-director'" ;;
    site-local-cache) echo "'topo-site-local-cache'" ;;
    transfer)         echo "'topo-transfer-server'" ;;
    director-ha)      echo "'director-ha'" ;;
    lab)              echo "'origin-ssh'" ;;
    idp)              echo "'auth-oidc-local'" ;;
    metadata)         echo "'origin-metadata' or 'origin-metadata-tx'" ;;
    monitoring)       echo "'with-grafana'" ;;
    webdav)           echo "'origin-https' or 'origin-httpsv2'" ;;
    s3)               echo "'origin-s3', 'origin-s3v2', or 'cache-tiered'" ;;
  esac
}

# Die if any of profiles $2... is enabled; $1 says why, as in "$1, so it
# cannot add profile ...".
fed_refuse_profiles() {
  fed_why=$1
  shift
  for fed_refused in "$@"; do
    if fed_has_profile "${fed_refused}"; then
      die "${fed_why}, so it cannot add profile '${fed_refused}'" \
          "($(fed_profile_presets "${fed_refused}"))"
    fi
  done
}

# Whether the origins run XRootD in front of their storage.
fed_xrootd_origin() {
  case "${ORIGIN_VARIANT}" in
    posix|s3|https) return 0 ;;
  esac
  return 1
}

# The tiny topology is one container, with no room for another service,
# and its cache is always V2. Of the profiles, only the metadata receiver
# and the identity provider work with it.
if [ "${fed_topology}" = tiny ]; then
  for fed_profile in ${fed_profiles}; do
    case "${fed_profile}" in
      metadata|idp) ;;
      *) fed_refuse_profiles "the 'tiny' topology is one container" "${fed_profile}" ;;
    esac
  done
  [ "${CACHE_VARIANT}" = v2 ] \
      || die "the 'tiny' topology's cache is always V2, so CACHE_VARIANT" \
             "('cache-xrootd') must be 'v2', not '${CACHE_VARIANT}'"
  [ "${EXTERNAL_ISSUER}" = false ] \
      || die "the 'tiny' topology has no discovery host to serve the external" \
             "issuer ('auth-external-issuer')"
  [ "${DIRECTOR_DISCOVERY}" = false ] \
      || die "the 'tiny' topology's director is its discovery host already, so" \
             "DIRECTOR_DISCOVERY ('director-discovery') must be 'false'"
fi

# A standalone origin is one origin with no director, registry, or cache
# (config/standalone_origin.go in Pelican). It refuses XRootD and
# Origin.DisableDirectClients, and with no registry, the metadata
# verifier has no keys to check events against.
if [ "${fed_topology}" = standalone ]; then
  fed_refuse_profiles "the 'standalone' topology is one origin and no federation" \
      multi-origin multi-cache multi-owner multi-director site-local-cache transfer metadata
  ! fed_xrootd_origin \
      || die "Pelican runs a standalone origin ('topo-standalone') only on a native" \
             "backend, not '${ORIGIN_VARIANT}'"
  [ "${CACHE_VARIANT}" = v2 ] \
      || die "the 'standalone' topology has no cache, so CACHE_VARIANT" \
             "('cache-xrootd') must be 'v2', not '${CACHE_VARIANT}'"
  [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = false ] \
      || die "a standalone origin has only direct clients, so Pelican refuses" \
             "ORIGIN_DISABLE_DIRECT_CLIENTS ('origin-no-direct')"
  [ "${DIRECTOR_DEFAULT_RESPONSE}" = cache ] \
      || die "the 'standalone' topology has no director, so DIRECTOR_DEFAULT_RESPONSE" \
             "('director-default-origin') must be 'cache'"
  [ "${REGISTRY_REQUIRE_APPROVAL}" = false ] \
      || die "the 'standalone' topology has no registry, so it cannot require" \
             "approval ('registry-approval')"
  [ "${DIRECTOR_DISCOVERY}" = false ] \
      || die "the 'standalone' topology has no director to serve the federation's" \
             "metadata, so DIRECTOR_DISCOVERY ('director-discovery') must be 'false'"
fi

# Without its backend, an origin comes up healthy with no storage.
case "${ORIGIN_VARIANT}" in
  ssh)           fed_has_profile lab \
                     || die "an 'ssh' origin needs the lab server; use '-p origin-ssh'" ;;
  https|httpsv2) fed_has_profile webdav \
                     || die "an '${ORIGIN_VARIANT}' origin needs its WebDAV server;" \
                            "use '-p origin-${ORIGIN_VARIANT}'" ;;
  s3|s3v2)       fed_has_profile s3 \
                     || die "an '${ORIGIN_VARIANT}' origin needs its S3 server;" \
                            "use '-p origin-${ORIGIN_VARIANT}'" ;;
esac

# Pelican's HTTPS backends take only one export.
case "${ORIGIN_VARIANT}" in
  https|httpsv2)
    [ "$(fed_count_namespaces)" -eq 1 ] \
        || die "an '${ORIGIN_VARIANT}' origin takes one export, but" \
               "ORIGIN_NAMESPACES is '${fed_namespaces}'" ;;
esac

# The connection broker relays to the origin's XRootD
# (origin/broker_client.go), only for an origin with one export
# (origin/advertise.go), and here only for origin-0 in a container of
# its own.
if [ "${ORIGIN_BROKER}" = true ]; then
  fed_xrootd_origin \
      || die "ORIGIN_BROKER ('origin-broker') needs an XRootD origin ('origin-xrootd'," \
             "'origin-s3', or 'origin-https'), not '${ORIGIN_VARIANT}'"
  [ "$(fed_count_namespaces)" -eq 1 ] \
      || die "ORIGIN_BROKER ('origin-broker') takes one namespace, since Pelican" \
             "brokers only an origin with one export, but ORIGIN_NAMESPACES is" \
             "'${fed_namespaces}'"
  [ "${fed_topology}" = full ] \
      || die "ORIGIN_BROKER ('origin-broker') needs the 'full' topology, not '${fed_topology}'"
  fed_refuse_profiles "ORIGIN_BROKER ('origin-broker') brokers origin-0 alone" \
      multi-origin multi-owner
fi

# Behind a firewall (`origin-firewalled`), origin-0 is on a network the
# caches are not on, so they reach it only through the broker, for which
# only an XRootD cache has a client (cache/broker_client.go in Pelican):
# there must be a cache, and every cache must be XRootD's. The caches
# verify a token with keys from its issuer, and the origins' own issuer
# is out of their reach, so the exports must trust an issuer they can
# reach, or take no token.
if [ "${ORIGIN_FIREWALLED}" = true ]; then
  [ "${ORIGIN_BROKER}" = true ] \
      || die "ORIGIN_FIREWALLED ('origin-firewalled') cuts origin-0 off from the caches, which" \
             "then reach it only through the connection broker (ORIGIN_BROKER, 'origin-broker')"
  [ "${FEDERATED_CACHE}" = true ] \
      || die "ORIGIN_FIREWALLED ('origin-firewalled') needs a cache to reach origin-0 through" \
             "the broker, and FEDERATED_CACHE ('topo-no-cache') runs none"
  [ "${CACHE_VARIANT}" = xrootd ] && [ "${CACHE_1_VARIANT}" = xrootd ] \
      || die "ORIGIN_FIREWALLED ('origin-firewalled') needs every cache XRootD's ('cache-xrootd')," \
             "which alone has a broker client; a V2 cache could not reach origin-0 at all"
  if [ "${EXTERNAL_ISSUER}" = false ] && [ "${ORIGIN_PUBLIC_ONLY}" = false ]; then
    die "ORIGIN_FIREWALLED ('origin-firewalled') puts the origins' own issuer out of the caches'" \
        "reach, so they could verify no token: the exports must trust an issuer they can" \
        "reach ('auth-external-issuer'), or take none ('origin-public')"
  fi
fi

# The identity provider (`auth-oidc-local`) and CILogon
# (`auth-oidc-cilogon`) each serve the servers' OIDC login, so each needs
# ENABLE_OIDC; a preset of your own may set it with neither, for another
# provider. CILogon knows only the clients registered with it, so its
# client must be yours, not one made at random.
if fed_has_profile idp && [ "${ENABLE_OIDC}" = false ]; then
  die "the identity provider ('auth-oidc-local') serves an OIDC login, which ENABLE_OIDC turns off"
fi
if [ "${OIDC_CILOGON}" = true ]; then
  [ "${ENABLE_OIDC}" = true ] \
      || die "OIDC_CILOGON ('auth-oidc-cilogon') serves an OIDC login, which ENABLE_OIDC turns off"
  ! fed_has_profile idp \
      || die "OIDC_CILOGON ('auth-oidc-cilogon') and the identity provider ('auth-oidc-local')" \
             "each serve the servers' OIDC login; choose one"
  for fed_f in oidc-client-id oidc-client-secret; do
    [ -s "local/etc/${fed_f}" ] \
        || die "OIDC_CILOGON ('auth-oidc-cilogon') needs your CILogon client's ID and secret" \
               "in local/etc/oidc-client-{id,secret}; local/etc/${fed_f} is missing or empty"
  done
fi

# A transfer server (`topo-transfer-server`) is the transfer API in a
# process of its own (`pelican-server serve --module transfer`), which the
# transfer-api suite tests in place of origin-0's: one of the two at a
# time.
if [ "${TRANSFER_SERVER}" = true ]; then
  fed_has_profile transfer \
      || die "TRANSFER_SERVER needs the transfer server; use '-p topo-transfer-server'"
  [ "${fed_topology}" = full ] \
      || die "TRANSFER_SERVER ('topo-transfer-server') needs the 'full' topology," \
             "not '${fed_topology}'"
  [ "${ORIGIN_TRANSFER_API}" = false ] \
      || die "TRANSFER_SERVER ('topo-transfer-server') and ORIGIN_TRANSFER_API" \
             "('origin-transfer-api') each serve a transfer API, of which the" \
             "transfer-api suite tests one; choose one"
fi

# A load balancer in front of the directors (`director-ha`) balances two
# of them, in the full topology, and hides which one a request reaches,
# which the connection broker cannot abide: an origin polls one
# director's broker, and a request for a brokered connection must reach
# that same director.
if [ "${DIRECTOR_HA}" = true ]; then
  fed_has_profile director-ha \
      || die "DIRECTOR_HA needs the load balancer; use '-p director-ha'"
  [ "${fed_topology}" = full ] \
      || die "DIRECTOR_HA ('director-ha') needs the 'full' topology, not '${fed_topology}'"
  fed_has_profile multi-director \
      || die "DIRECTOR_HA ('director-ha') balances two directors, so it needs" \
             "'topo-multi-director'"
  [ "${ORIGIN_BROKER}" = false ] \
      || die "DIRECTOR_HA ('director-ha') hides which director a request reaches, which" \
             "the connection broker ('origin-broker') needs to know; choose one"
fi

# Both configure the origins' embedded issuer, which `auth-no-issuer`
# turns off, as does the external issuer; and the per-export templates
# go on /protected-b.
if [ "${ORIGIN_ENABLE_ISSUER}" = false ]; then
  [ "${ISSUER_SHORT_TOKENS}" = false ] \
      || die "ISSUER_SHORT_TOKENS ('origin-short-tokens') shortens the origins' issuer's" \
             "tokens, but the issuer is off (ORIGIN_ENABLE_ISSUER, 'auth-no-issuer' or" \
             "'auth-external-issuer')"
  [ "${ORIGIN_EXPORT_TEMPLATES}" = false ] \
      || die "ORIGIN_EXPORT_TEMPLATES ('origin-export-templates') gives the origins'" \
             "issuer templates, but the issuer is off (ORIGIN_ENABLE_ISSUER," \
             "'auth-no-issuer' or 'auth-external-issuer')"
fi
if [ "${ORIGIN_EXPORT_TEMPLATES}" = true ]; then
  fed_exports_namespace protected-b \
      || die "ORIGIN_EXPORT_TEMPLATES ('origin-export-templates') gives /protected-b" \
             "templates of its own, but ORIGIN_NAMESPACES ('${fed_namespaces}') lacks it"
fi

# Only the posixv2 origin has POSC and publishes metadata.
if [ "${ORIGIN_POSC}" = true ] || [ "${ORIGIN_METADATA}" = true ]; then
  [ "${ORIGIN_VARIANT}" = posixv2 ] \
      || die "POSC ('origin-posc') and metadata publishing ('origin-metadata') need" \
             "the 'posixv2' origin, not '${ORIGIN_VARIANT}'"
fi

# XRootD's POSC plugin (libXrdOssPosc) serves only its posix origin.
if [ "${ORIGIN_ATOMIC_UPLOADS}" = true ]; then
  [ "${ORIGIN_VARIANT}" = posix ] \
      || die "ORIGIN_ATOMIC_UPLOADS ('origin-atomic-uploads') needs the XRootD posix" \
             "origin ('origin-xrootd'), not '${ORIGIN_VARIANT}'"
fi

# Only the native origins send Origin.CacheControl (origin_serve/ in
# Pelican); an XRootD origin ignores it.
if [ -n "${ORIGIN_CACHE_CONTROL}" ]; then
  ! fed_xrootd_origin \
      || die "ORIGIN_CACHE_CONTROL ('origin-max-age') needs a native origin, not" \
             "'${ORIGIN_VARIANT}', since XRootD's send no Origin.CacheControl"
fi
if [ "${ORIGIN_METADATA}" = true ]; then
  fed_has_profile metadata \
      || die "ORIGIN_METADATA needs the metadata receiver; use '-p origin-metadata'"
fi

# The SSH tunnel carries an ssh origin's requests to its helper.
if [ "${ORIGIN_SSH_TUNNEL}" = true ]; then
  [ "${ORIGIN_VARIANT}" = ssh ] \
      || die "ORIGIN_SSH_TUNNEL ('origin-ssh-tunnel') tunnels an ssh origin's requests to" \
             "its helper, so it needs the 'ssh' origin ('origin-ssh'), not '${ORIGIN_VARIANT}'"
fi

# Pelican refuses DisableDirectClients with exports that mix public and
# token-protected reads.
if [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = true ]; then
  [ "$(fed_count_namespaces)" -eq 1 ] \
      || die "ORIGIN_DISABLE_DIRECT_CLIENTS ('origin-no-direct') takes one" \
             "namespace, but ORIGIN_NAMESPACES is '${fed_namespaces}'"
fi

# /public takes writes (`origin-public-writes`) only where it is exported,
# and where the origins take direct clients, since Pelican allows only
# reads without them. A pstore origin's /public reads /protected-a's
# storage (see fed_storage_dir), where a write to it would then land.
if [ "${ORIGIN_PUBLIC_WRITES}" = true ]; then
  fed_exports_namespace public \
      || die "ORIGIN_PUBLIC_WRITES ('origin-public-writes') gives /public writes, but" \
             "ORIGIN_NAMESPACES ('${fed_namespaces}') has no /public; add 'origin-public'"
  [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = false ] \
      || die "ORIGIN_PUBLIC_WRITES ('origin-public-writes') gives /public writes, which" \
             "ORIGIN_DISABLE_DIRECT_CLIENTS ('origin-no-direct') takes away"
  [ "${ORIGIN_VARIANT}" != pstore ] \
      || die "ORIGIN_PUBLIC_WRITES ('origin-public-writes') gives /public writes, but a" \
             "'pstore' origin's /public reads /protected-a's storage"
fi

# Nothing but the running origin can write a pstore, so the transfers
# suite seeds each protected namespace through its writes. /public reads
# /protected-a's storage (see fed_storage_dir).
if [ "${ORIGIN_VARIANT}" = pstore ]; then
  for fed_ns in ${fed_namespaces}; do
    [ "${fed_ns}" != public ] || continue
    fed_namespace_caps "${fed_ns}"
    case " ${fed_caps} " in
      *" Writes "*) ;;
      *) die "a 'pstore' origin's /${fed_ns} needs writes, which" \
             "'origin-no-direct' takes away" ;;
    esac
  done
  if fed_exports_namespace public && ! fed_exports_namespace protected-a; then
    die "a 'pstore' origin's /public reads /protected-a's storage, but" \
        "ORIGIN_NAMESPACES ('${fed_namespaces}') has no /protected-a ('origin-public')"
  fi
fi

# origin-2 has no store of its own on the lab server, and no plain copy
# for the transfers suite to seed a pstore from. Its exports mirror /public
# and /protected-a (see fed_generate).
if fed_has_profile multi-owner; then
  case "${ORIGIN_VARIANT}" in
    ssh|pstore)
        die "a second owner ('topo-multi-owner') has no store with an" \
            "'${ORIGIN_VARIANT}' origin" ;;
  esac
  fed_exports_namespace public || fed_exports_namespace protected-a \
      || die "a second owner ('topo-multi-owner') mirrors /public and" \
             "/protected-a, but ORIGIN_NAMESPACES ('${fed_namespaces}') has neither"
  # With /public exported, it exports two prefixes inside it, which
  # Pelican's HTTPS backends refuse (see above); without, only /other.
  case "${ORIGIN_VARIANT}" in
    https|httpsv2)
        ! fed_exports_namespace public \
            || die "a second owner ('topo-multi-owner') exports /public/other and" \
                   "/public/secret, but an '${ORIGIN_VARIANT}' origin takes one export;" \
                   "drop 'origin-public'" ;;
  esac
  # Its /public/secret takes a token beside its public exports, which
  # Pelican refuses once the origins serve only caches; with /protected-a
  # exported too, it exports only /other, and no mix.
  if [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = true ] && fed_exports_namespace public; then
    die "a second owner ('topo-multi-owner') exports /public/secret, which takes a token," \
        "beside its public exports, which Pelican refuses once the origins serve only" \
        "caches ('origin-no-direct'); drop 'origin-public'"
  fi
fi

# Multiuser switches to each user's IDs, which needs root and a POSIX
# store: posixv2 does it itself, and XRootD's posix origin through its
# multiuser plugin (libXrdMultiuser).
if [ "${ORIGIN_MULTIUSER}" = true ]; then
  case "${ORIGIN_VARIANT}" in
    posixv2|posix) ;;
    *) die "ORIGIN_MULTIUSER ('origin-multiuser') needs a POSIX origin ('origin-posixv2'" \
           "or 'origin-xrootd'), not '${ORIGIN_VARIANT}'" ;;
  esac
  [ "${SERVER_DROP_PRIVILEGES}" = false ] \
      || die "ORIGIN_MULTIUSER ('origin-multiuser') needs root, which" \
             "SERVER_DROP_PRIVILEGES ('server-unprivileged') gives up"
fi

# The ssh origin's private key is yours, mode 0600, so the pelican user
# could not read it.
if [ "${SERVER_DROP_PRIVILEGES}" = true ] && [ "${ORIGIN_VARIANT}" = ssh ]; then
  die "an 'ssh' origin's key is unreadable once SERVER_DROP_PRIVILEGES" \
      "('server-unprivileged') drops to the pelican user"
fi

# Mixed kinds of cache (`cache-mixed`): cache-1 is of another kind than
# the rest, so it must run, which only the full topology's multi-cache
# profile has it do.
if [ "${fed_cache_mixed}" = true ]; then
  [ "${CACHE_1_VARIANT}" != "${CACHE_VARIANT}" ] \
      || die "CACHE_1_VARIANT ('cache-mixed') is '${CACHE_1_VARIANT}', the same as" \
             "CACHE_VARIANT, so nothing is mixed; drop one of the presets"
  if [ "${fed_topology}" != full ] || ! fed_has_profile multi-cache; then
    die "CACHE_1_VARIANT ('cache-mixed') is cache-1's, which only the 'full'" \
        "topology runs, with 'topo-multi-cache'"
  fi
fi

# No cache at all (`topo-no-cache`): the full topology without cache-0,
# so nothing that adds a cache, or configures one, and not the origins
# that serve only caches, which would then serve no one. The kind of
# cache is moot, so it must stay the default, as for a standalone origin.
if [ "${FEDERATED_CACHE}" = false ]; then
  [ "${fed_topology}" = full ] \
      || die "FEDERATED_CACHE ('topo-no-cache') is the 'full' topology's cache-0, which" \
             "the '${fed_topology}' topology does not run"
  fed_refuse_profiles "FEDERATED_CACHE ('topo-no-cache') runs no cache" \
      multi-cache site-local-cache
  [ "${CACHE_VARIANT}" = v2 ] \
      || die "FEDERATED_CACHE ('topo-no-cache') runs no cache, so CACHE_VARIANT" \
             "('cache-xrootd') must be 'v2', not '${CACHE_VARIANT}'"
  [ "${fed_cache_mixed}" = false ] \
      || die "FEDERATED_CACHE ('topo-no-cache') runs no cache, so none can be mixed" \
             "('cache-mixed')"
  [ "${CACHE_TIERING}" = false ] \
      || die "FEDERATED_CACHE ('topo-no-cache') runs no cache to tier ('cache-tiered')"
  [ -z "${CACHE_MEMORY_SIZE}${CACHE_HIGH_WATER_MIB}${CACHE_LOW_WATER_MIB}" ] \
      || die "FEDERATED_CACHE ('topo-no-cache') runs no cache to give a memory cache" \
             "('cache-memory') or a bounded store ('cache-small')"
  [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = false ] \
      || die "FEDERATED_CACHE ('topo-no-cache') runs no cache, which is all that" \
             "ORIGIN_DISABLE_DIRECT_CLIENTS ('origin-no-direct') lets the origins serve"
fi

# Cache tiering is the V2 cache's, here only in the full topology, where
# the `s3` service holds the tiered objects.
if [ "${CACHE_TIERING}" = true ]; then
  fed_require_v2_caches "CACHE_TIERING ('cache-tiered')"
  [ "${fed_topology}" = full ] \
      || die "CACHE_TIERING ('cache-tiered') needs the 'full' topology, not '${fed_topology}'"
  fed_has_profile s3 \
      || die "CACHE_TIERING needs the S3 server; use '-p cache-tiered'"
fi

# The memory cache is the V2 cache's too; either kind's store may be
# bounded, each its own way (see fed_generate). A standalone origin has
# no cache.
if [ -n "${CACHE_MEMORY_SIZE}" ]; then
  fed_require_v2_caches "CACHE_MEMORY_SIZE ('cache-memory')"
fi
if [ -n "${CACHE_MEMORY_SIZE}${CACHE_HIGH_WATER_MIB}${CACHE_LOW_WATER_MIB}" ]; then
  [ "${fed_topology}" != standalone ] \
      || die "CACHE_MEMORY_SIZE ('cache-memory') and CACHE_HIGH_WATER_MIB ('cache-small')" \
             "need a cache, which the 'standalone' topology lacks"
fi
case "${CACHE_MEMORY_SIZE}" in
  ""|[0-9]*[0-9KMGB]) ;;
  *) die "CACHE_MEMORY_SIZE must be a size such as '64MB', not '${CACHE_MEMORY_SIZE}'" ;;
esac
# fed_generate writes both marks, and the tests read them, so both are set
# or neither.
if [ -n "${CACHE_HIGH_WATER_MIB}${CACHE_LOW_WATER_MIB}" ]; then
  for fed_mark in "${CACHE_HIGH_WATER_MIB}" "${CACHE_LOW_WATER_MIB}"; do
    case "${fed_mark}" in
      [1-9]|[1-9][0-9]*) ;;
      *) die "CACHE_HIGH_WATER_MIB and CACHE_LOW_WATER_MIB must both be whole numbers" \
             "of MiB, not '${CACHE_HIGH_WATER_MIB}' and '${CACHE_LOW_WATER_MIB}'" ;;
    esac
  done
  [ "${CACHE_LOW_WATER_MIB}" -lt "${CACHE_HIGH_WATER_MIB}" ] \
      || die "CACHE_LOW_WATER_MIB (${CACHE_LOW_WATER_MIB}) must be below" \
             "CACHE_HIGH_WATER_MIB (${CACHE_HIGH_WATER_MIB})"
fi

# The profiles that fed.sh enables itself (see fed_profiles), after the
# checks above, which consider only the presets'.
if [ "${fed_topology}" = full ]; then
  fed_add_profile federation
  if [ "${FEDERATED_CACHE}" = true ]; then
    fed_add_profile cache
  fi
  if [ "${DIRECTOR_DISCOVERY}" = false ]; then
    fed_add_profile discovery
  fi
fi
if [ "${fed_topology}" != tiny ] && [ "${EXTERNAL_ISSUER}" = true ]; then
  fed_add_profile external-issuer
fi

# The federation's discovery URL, from which fed_generate builds the
# rest, and which the metadata verifier takes from the environment
# (docker-compose.yaml): the `discovery` service's; director-0's
# (`director-discovery`), or the directors' shared name (with
# `director-ha` too); in `tiny`, the one host's; for a standalone origin,
# its own.
case "${fed_topology}" in
  tiny)       FED_DISCOVERY_URL=https://fed:8444 ;;
  standalone) FED_DISCOVERY_URL=https://origin-0:8444 ;;
  *)          FED_DISCOVERY_URL=https://discovery:8444
              if [ "${DIRECTOR_DISCOVERY}" = true ]; then
                FED_DISCOVERY_URL=https://director-0:8444
                if [ "${DIRECTOR_HA}" = true ]; then
                  FED_DISCOVERY_URL=https://director:8444
                fi
              fi ;;
esac
export FED_DISCOVERY_URL

# `check` asks only whether the checks above pass.
if [ "$1" = check ]; then
  [ $# -eq 1 ] || die_usage "check takes no arguments"
  exit 0
fi

# Remember the selection only once it has passed the checks above.
if [ -n "${presets_given}" ]; then
  printf '%s\n' "${presets}" >framework/var/generated/presets
fi

# The dev container's /app: the Pelican checkout that PELICAN_SRC_DIR
# names, if any, which the container may write; otherwise app_empty, an
# empty volume. Not this checkout, nor one that holds it: the container
# must not write what the host runs (see the dev service in
# docker-compose.yaml).
if [ -n "${PELICAN_SRC_DIR:-}" ]; then
  DEV_APP_SOURCE=$(CDPATH="" cd -- "${PELICAN_SRC_DIR}" 2>/dev/null && pwd -P) \
      || die "PELICAN_SRC_DIR is not a directory: ${PELICAN_SRC_DIR}"
  fed_top=$(pwd -P)
  case "${DEV_APP_SOURCE}/" in
    "${fed_top}/"*) die "PELICAN_SRC_DIR must be outside this checkout: ${PELICAN_SRC_DIR}" ;;
  esac
  case "${fed_top}/" in
    "${DEV_APP_SOURCE}/"*) die "PELICAN_SRC_DIR must not hold this checkout: ${PELICAN_SRC_DIR}" ;;
  esac
else
  DEV_APP_SOURCE=app_empty
fi
export DEV_APP_SOURCE

#---------------------------------------------------------------------------
# Generated files, rewritten on every invocation.

# Write stdin to $1 only if it changed. In place, not by rename: some of
# these are bind-mounted as single files, which Docker binds by inode.
fed_write() {
  fed_write_tmp="$1.new"
  cat >"${fed_write_tmp}"
  if [ ! -f "$1" ] || ! cmp -s "${fed_write_tmp}" "$1"; then
    cat "${fed_write_tmp}" >"$1"
  fi
  rm -f "${fed_write_tmp}"
}

# Write value $2 as the one line of framework/var/generated/$1.
fed_write_value() {
  printf '%s\n' "$2" | fed_write "framework/var/generated/$1"
}

# The head of a generated configuration layer, which the caller follows
# with a blank line and the settings.
fed_layer_head() {
  printf -- '---\n'
  printf '# Generated by %s. Do not edit.\n' "${progname}"
}

# The arguments as a YAML flow sequence of strings, e.g.
# ["Reads", "Writes"].
fed_yaml_list() {
  sep=""
  printf '['
  for item in "$@"; do
    printf '%s"%s"' "${sep}" "${item}"
    sep=", "
  done
  printf ']'
}

# The external token issuer (`auth-external-issuer`), which the discovery
# host serves from framework/var/generated/issuer/.
fed_external_issuer=https://discovery:8444/issuer

# The issuer URL an origin advertises for federation prefix $1, derived
# as Pelican does. Tokens must match it exactly.
#
#   external issuer  fed_external_issuer, which the exports name
#   issuer enabled   <origin>/api/v1.0/issuer/ns<prefix>
#   no issuer        <origin>, or <origin>/api/v1.0/origin when the origin
#                    shares a process with a director (`tiny`)
fed_issuer_url() {
  if [ "${EXTERNAL_ISSUER}" = true ]; then
    printf '%s\n' "${fed_external_issuer}"
    return 0
  fi
  case "${ORIGIN_ENABLE_ISSUER}" in
    true)
        fed_own_issuer_url "${fed_origin_url}" "$1" ;;
    *)
        if [ "${fed_topology}" = tiny ]; then
          printf '%s/api/v1.0/origin\n' "${fed_origin_url}"
        else
          printf '%s\n' "${fed_origin_url}"
        fi ;;
  esac
}

# The issuer URL of an origin at web URL $1 with its own issuer enabled,
# for federation prefix $2.
fed_own_issuer_url() {
  printf '%s/api/v1.0/issuer/ns%s\n' "$1" "$2"
}

# origin-2's exports (`topo-multi-owner`), one per line: its federation
# prefix, and the origins' namespace that it mirrors, whose capabilities
# it has. /public/other mirrors /public, nested in it, and /other
# /protected-a; nothing mirrors /protected-b. Where /public is exported,
# /public/secret, also nested in it, mirrors nothing (`-`) but has
# /protected-a's capabilities: a protected prefix inside a public one,
# for the auth suite's nested-protected check. It is not inside
# /public/other, since one native origin cannot serve one export inside
# another (their routes conflict).
fed_owner_exports() {
  for ns in ${fed_namespaces}; do
    case "${ns}" in
      public)      printf '/public/other public\n' ;;
      protected-a) printf '/other protected-a\n' ;;
    esac
  done
  if fed_exports_namespace public; then
    printf '/public/secret -\n'
  fi
}

# The namespace whose capabilities origin-2's export mirroring $1 has (see
# fed_owner_exports).
fed_owner_caps_of() {
  case "$1" in
    -) printf 'protected-a\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

# Make directory $2 hold a copy of what directory $1 holds, or nothing if
# $1 is missing. $2 is emptied rather than replaced, because a running
# container's bind mount refers to it by inode.
fed_copy_dir() {
  mkdir -p "$2"
  find "$2" -mindepth 1 -delete
  if [ -d "$1" ]; then
    cp -R "$1/." "$2/"
  fi
}

# A line of framework/var/generated/origins for service $1, storing in
# framework/var/data/origin/$2. An XRootD origin serves its exports on a
# port of its own. A native origin serves them on the web port, at the
# bare prefix unless a director shares its process (`tiny`).
fed_origin_line() {
  if fed_xrootd_origin; then
    url="https://$1:8443"
  elif [ "${fed_topology}" = tiny ]; then
    url="https://$1:8444/api/v1.0/origin/data"
  else
    url="https://$1:8444"
  fi
  case "${ORIGIN_VARIANT}" in
    ssh)    printf '%s %s data/lab-server\n' "$1" "${url}" ;;
    pstore) printf '%s %s data/origin/%s pstore\n' "$1" "${url}" "$2" ;;
    *)      printf '%s %s data/origin/%s\n' "$1" "${url}" "$2" ;;
  esac
}

# A line of framework/var/generated/caches for service $1: its data URL;
# its kind, v2 (native) or xrootd; and its role, `federated` or, for $2
# `site-local`, a cache that the director does not know of. An XRootD
# cache serves on a port of its own. The `tiny` topology's cache is
# always native and shares a process with the director, so it serves
# only under /api/v1.0/cache/data/<discovery host>.
fed_cache_line() {
  role=${2:-federated}
  variant=${CACHE_VARIANT}
  if [ "$1" = cache-1 ]; then
    variant=${CACHE_1_VARIANT}
  fi
  if [ "${fed_topology}" = tiny ]; then
    printf '%s https://%s:8444/api/v1.0/cache/data/%s v2 %s\n' \
        "$1" "$1" "${fed_discovery_url#https://}" "${role}"
  elif [ "${variant}" = xrootd ]; then
    printf '%s https://%s:8442 xrootd %s\n' "$1" "$1" "${role}"
  else
    printf '%s https://%s:8444 v2 %s\n' "$1" "$1" "${role}"
  fi
}

# Write stdin to file $2 in service $1's generated layer,
# framework/var/generated/instance/<svc>/ (its /fed/generated.instance).
# fed_generate makes the directories, which are never replaced, since
# each is bind-mounted.
fed_instance_write() {
  fed_write "framework/var/generated/instance/$1/$2"
}

# Remove file $2 from service $1's generated layer.
fed_instance_rm() {
  rm -f "framework/var/generated/instance/$1/$2"
}

# The directory of export $1's storage in an origin's store
# (framework/var/data/origin/<N>, or data/lab-server under `origin-ssh`):
# its federation prefix, without the leading slash and with any other as
# a dash, e.g. public-other for /public/other (common.storage_dir() in the
# tests). Each namespace has storage of its own, but a pstore's /public
# reads /protected-a's, since nothing can write /public.
fed_storage_dir() {
  case "${ORIGIN_VARIANT}:$1" in
    pstore:/public) printf 'protected-a\n' ;;
    *)              printf '%s\n' "$1" | sed 's|^/||; s|/|-|g' ;;
  esac
}

# The StoragePrefix of export $1, which leads to its directory (see
# fed_storage_dir). A pstore's is a path in its catalog. The backends of
# the https, httpsv2, and s3v2 origins serve an origin's store at the root
# of its URL or bucket. An s3 origin ignores StoragePrefix, and is given a
# bucket per export instead (fed_s3_bucket). Every other origin mounts its
# store at /data.
fed_storage_prefix() {
  case "${ORIGIN_VARIANT}" in
    s3)                        printf '/\n' ;;
    pstore|https|httpsv2|s3v2) printf '/%s\n' "$(fed_storage_dir "$1")" ;;
    *)                         printf '/data/%s\n' "$(fed_storage_dir "$1")" ;;
  esac
}

# Under `origin-s3`, the bucket of service $1's export $2: the s3 service
# serves the export's directory (fed_storage_dir) as bucket
# <service>-<directory> (see docker-compose.yaml).
fed_s3_bucket() {
  printf '%s-%s\n' "$1" "$(fed_storage_dir "$2")"
}

# One entry of Origin.Exports: federation prefix $1, with namespace $2's
# capabilities (see fed_namespace_caps) and storage prefix $3. If Pelican
# gives it an issuer, which it does only for an export that takes writes
# or needs a token to read, $4 is the issuer URL to pin and $5 the JWKS
# file whose keys that issuer adds; either may be empty. $6, if given, is
# its S3 bucket. Under `origin-export-templates`, /protected-b gets the
# issuer suite's authorization templates, in place of the global
# Issuer.AuthorizationTemplates (framework/config.d/base/40-origin.yaml).
fed_export_entry() {
  fed_namespace_caps "$2"
  printf -- '    - FederationPrefix: %s\n' "$1"
  # shellcheck disable=SC2086  # one capability per word
  printf '      Capabilities: %s\n' "$(fed_yaml_list ${fed_caps})"
  case " ${fed_caps} " in
    *" Writes "*|*" Reads "*)
        [ -z "$4" ] || printf '      IssuerUrls: ["%s"]\n' "$4"
        [ -z "$5" ] || printf '      IssuerJwks: %s\n' "$5" ;;
  esac
  printf '      StoragePrefix: %s\n' "$3"
  [ -z "${6:-}" ] || printf '      S3Bucket: %s\n' "$6"
  if [ "${ORIGIN_EXPORT_TEMPLATES}" = true ] && [ "$1" = /protected-b ]; then
    printf '      AuthorizationTemplates:\n'
    printf '        - prefix: /data/issuer/export\n'
    printf '          actions: [read]\n'
    printf '          users: [test-stranger]\n'
  fi
}

# The head of an Origin.Exports layer.
fed_exports_header() {
  fed_layer_head
  printf '\nOrigin:\n  Exports:\n'
}

# The origins' exports, of the namespaces in ORIGIN_NAMESPACES, as a
# configuration layer, in full: a later layer replaces a list rather than
# merging into it. With $1, a service, under `origin-s3`, each names its
# own bucket (fed_s3_bucket). With $2 true, each pins its issuer URL (see
# fed_generate).
fed_origin_exports() {
  fed_exports_header
  first=yes
  for ns in ${fed_namespaces}; do
    [ -n "${first}" ] || printf '\n'
    first=""
    pinned=""
    if [ "$2" = true ]; then
      pinned=$(fed_issuer_url "/${ns}")
    fi
    # Only the protected namespaces have keys of their own (fed_key_init):
    # /public, where it takes writes (`origin-public-writes`), trusts the
    # origins' alone.
    jwks="/fed/issuer-jwks/ns-${ns}.jwks"
    if [ "${EXTERNAL_ISSUER}" = true ] || [ "${ns}" = public ]; then
      jwks=""
    fi
    bucket=""
    if [ -n "$1" ] && [ "${ORIGIN_VARIANT}" = s3 ]; then
      bucket=$(fed_s3_bucket "$1" "/${ns}")
    fi
    fed_export_entry "/${ns}" "${ns}" "$(fed_storage_prefix "/${ns}")" "${pinned}" \
        "${jwks}" "${bucket}"
  done
}

# Set fed_python to the Python that pyproject.toml requires, in the
# project's environment, which uv makes, fetching Python if need be.
# fed.sh runs it straight, not under `uv run`, so that
# framework/control.py's parent is fed.sh.
fed_require_python() {
  [ -z "${fed_python:-}" ] || return 0
  command -v uv >/dev/null 2>&1 \
      || die "uv is missing; see https://docs.astral.sh/uv/"
  fed_python=$(uv run --quiet --frozen --no-dev python -c 'import sys; print(sys.executable)') \
      || die "uv cannot provide the Python that pyproject.toml requires"
}

# The external issuer's JWKS: the public keys of
# framework/var/issuer-keys/issuer/, each a .jwks beside its .pem (see
# fed_key_make).
fed_write_issuer_jwks() {
  mkdir -p framework/var/generated/issuer
  fed_require_python
  "${fed_python}" - framework/var/issuer-keys/issuer <<'EOF' \
      | fed_write framework/var/generated/issuer/issuer.jwks
import glob, json, os, sys
keys = []
for public in sorted(glob.glob(os.path.join(sys.argv[1], "[0-9][0-9]-*.jwks"))):
    if os.path.exists(public[:-len(".jwks")] + ".pem"):
        with open(public) as f:
            keys += json.load(f)["keys"]
json.dump({"keys": keys}, sys.stdout, indent=2)
print()
EOF
}

# Write framework/var/generated/: the configuration layers that the
# presets imply, and what the tests read about the shape.
fed_generate() {
  # Every service, dev included, gets its federation URLs from here. In
  # `tiny`, one host is everything. A standalone origin is its own
  # discovery host, and has no director or registry. The federation's
  # JWKS is its directors', or the standalone origin's.
  fed_discovery_url=${FED_DISCOVERY_URL}
  case "${fed_topology}" in
    tiny)
      fed_director_url=https://fed:8444
      fed_registry_url=https://fed:8444
      directors=https://fed:8444
      fed_origin_url=https://fed:8444
      fed_origin_key_dir=issuer-keys/fed ;;
    standalone)
      fed_director_url=""
      fed_registry_url=""
      directors=""
      fed_origin_url=https://origin-0:8444
      fed_origin_key_dir=issuer-keys/origin-0 ;;
    *)
      # Clients use the directors' shared name (`director-ha`), where
      # there is one; the servers advertise to each director by its own.
      fed_director_url=https://director-0:8444
      if [ "${DIRECTOR_HA}" = true ]; then
        fed_director_url=https://director:8444
      fi
      fed_registry_url=https://registry:8444
      directors=https://director-0:8444
      if fed_has_profile multi-director; then
        directors="${directors} https://director-1:8444"
      fi
      fed_origin_url=https://origin-0:8444
      fed_origin_key_dir=issuer-keys/origin-0 ;;
  esac
  # The key that signs tokens the origins' exports accept.
  if [ "${EXTERNAL_ISSUER}" = true ]; then
    fed_origin_key_dir=issuer-keys/issuer
  fi
  fed_federation_url="pelican://${fed_discovery_url#https://}"
  fed_jwks_uri="${fed_director_url:-${fed_origin_url}}/.well-known/issuer.jwks"
  # The connection broker (`origin-broker`) is director-0's. A director
  # that serves the federation's metadata (`director-discovery`) names
  # its own broker, which is on by default (Director.EnableBroker),
  # whether or not an origin uses it.
  fed_broker_url=""
  if [ "${ORIGIN_BROKER}" = true ] || [ "${DIRECTOR_DISCOVERY}" = true ]; then
    fed_broker_url=${fed_director_url}
  fi

  # Pelican refuses a standalone origin that is told where a federation is.
  {
    fed_layer_head
    if [ "${fed_topology}" != standalone ]; then
      printf '\nFederation:\n'
      printf '  DiscoveryUrl: %s\n' "${fed_discovery_url}"
      printf '  DirectorUrl: %s\n' "${fed_director_url}"
      printf '  RegistryUrl: %s\n\n' "${fed_registry_url}"
      printf 'Server:\n  DirectorUrls:\n'
      for d in ${directors}; do printf '    - %s\n' "${d}"; done
    fi
  } | fed_write framework/var/generated/conf/50-topology.yaml

  # Knobs that nothing else depends on, so a later layer may override
  # them, e.g. for one service. Every server acts on the EnableOIDC flags
  # of the modules it runs.
  {
    fed_layer_head
    printf '\nLogging:\n  Level: %s\n' "${PELICAN_LOG_LEVEL}"
    for module in Origin Cache Director Registry; do
      printf '\n%s:\n  EnableOIDC: %s\n' "${module}" "${ENABLE_OIDC}"
    done
  } | fed_write framework/var/generated/conf/50-knobs.yaml

  # The servers' OIDC provider, found by discovery at OIDC.Issuer: the
  # identity provider (`auth-oidc-local`), from whose `groups` claim a
  # server takes its users' groups where Issuer.GroupSource is `oidc`
  # (which only the oidc suite sets, for a while); or CILogon
  # (`auth-oidc-cilogon`). generated/oidc names the framework's own
  # provider, the only one the tests can log in through, or says `off`.
  if fed_has_profile idp; then
    {
      fed_layer_head
      printf '\nOIDC:\n  Issuer: https://idp:9007\n'
      printf 'Issuer:\n  OIDCGroupClaim: groups\n'
    } | fed_write framework/var/generated/conf/60-oidc.yaml
    fed_write_value oidc https://idp:9007
  elif [ "${OIDC_CILOGON}" = true ]; then
    {
      fed_layer_head
      printf '\nOIDC:\n  Issuer: https://cilogon.org\n'
    } | fed_write framework/var/generated/conf/60-oidc.yaml
    fed_write_value oidc off
  else
    rm -f framework/var/generated/conf/60-oidc.yaml
    fed_write_value oidc off
  fi

  # The `discovery` service's documents. `tiny` and `standalone` run no
  # such service, but test.py reads discovery.json there too, for the
  # directors (none in `standalone`, as the origin's own document says).
  {
    printf '{\n'
    printf '  "discovery_endpoint": "%s",\n' "${fed_discovery_url}"
    printf '  "director_endpoint": "%s",\n' "${fed_director_url}"
    printf '  "director_advertise_endpoints": [\n'
    first=yes
    for d in ${directors}; do
      [ -n "${first}" ] || printf ',\n'
      first=""
      printf '    "%s"' "${d}"
    done
    printf '\n  ],\n'
    printf '  "namespace_registration_endpoint": "%s",\n' "${fed_registry_url}"
    printf '  "jwks_uri": "%s",\n' "${fed_jwks_uri}"
    printf '  "broker_endpoint": "%s"\n' "${fed_broker_url}"
    printf '}\n'
  } | fed_write framework/var/generated/discovery.json

  # The federation as a token issuer; see
  # framework/etc/nginx-discovery.conf.
  {
    printf '{\n'
    printf '  "issuer": "%s",\n' "${fed_discovery_url}"
    printf '  "jwks_uri": "%s"\n' "${fed_jwks_uri}"
    printf '}\n'
  } | fed_write framework/var/generated/openid-configuration.json

  # What test.py and init-data.py read about the shape. They run in
  # framework/var, hence the relative paths. issuer-urls has the issuer
  # URL the origins give each namespace, or would give it: /public, which
  # has none unless it takes writes (`origin-public-writes`), and any
  # namespace the shape does not export.
  for ns in public protected-a protected-b; do
    printf '%s %s\n' "${ns}" "$(fed_issuer_url "/${ns}")"
  done | fed_write framework/var/generated/issuer-urls
  fed_write_value origin-web-url "${fed_origin_url}"
  fed_write_value origin-key-dir "${fed_origin_key_dir}"
  fed_write_value external-issuer "${EXTERNAL_ISSUER}"
  fed_write_value federation-url "${fed_federation_url}"
  fed_write_value topology "${fed_topology}"
  fed_write_value origin-variant "${ORIGIN_VARIANT}"
  fed_write_value origin-broker "${ORIGIN_BROKER}"
  fed_write_value origin-firewalled "${ORIGIN_FIREWALLED}"
  fed_write_value director-default-response "${DIRECTOR_DEFAULT_RESPONSE}"
  fed_write_value registry-approval "${REGISTRY_REQUIRE_APPROVAL}"

  # The transfer API, origin-0's (`origin-transfer-api`) or a transfer
  # server's (`topo-transfer-server`): its URL; the issuer of its tokens
  # (origin-0's own even when the exports trust an external issuer, or
  # the transfer server's local issuer, its web URL); the directory of
  # the key that signs them; the service that serves it; and `exports`
  # where it confines jobs to the origin's exports, or `none` for a
  # transfer server, which confines them to nothing (transfer/transfer.go
  # in Pelican).
  if [ "${TRANSFER_SERVER}" = true ]; then
    printf '%s %s %s %s %s\n' https://transfer:8444 https://transfer:8444 \
        issuer-keys/transfer transfer none
  elif [ "${ORIGIN_TRANSFER_API}" = true ] && [ "${fed_topology}" = tiny ]; then
    printf '%s %s/api/v1.0/origin %s %s %s\n' "${fed_origin_url}" "${fed_origin_url}" \
        issuer-keys/fed fed exports
  elif [ "${ORIGIN_TRANSFER_API}" = true ]; then
    printf '%s %s %s %s %s\n' "${fed_origin_url}" "${fed_origin_url}" issuer-keys/origin-0 \
        origin-0 exports
  else
    printf 'off\n'
  fi | fed_write framework/var/generated/transfer-api
  fed_write_value origin-posc "${ORIGIN_POSC}"
  fed_write_value origin-enable-issuer "${ORIGIN_ENABLE_ISSUER}"
  fed_write_value origin-atomic-uploads "${ORIGIN_ATOMIC_UPLOADS}"
  fed_write_value origin-cache-control "${ORIGIN_CACHE_CONTROL}"
  fed_write_value origin-multiuser "${ORIGIN_MULTIUSER}"
  fed_write_value server-drop-privileges "${SERVER_DROP_PRIVILEGES}"
  if [ "${ORIGIN_METADATA}" = true ]; then
    printf '%s\n' "${ORIGIN_METADATA_MODE}"
  else
    printf 'off\n'
  fi | fed_write framework/var/generated/origin-metadata

  # The version that every server should report: PELICAN_TAG's, without
  # the `v`, if the shape runs only its images; `custom` if it runs any of
  # the user's own. `tiny` and `standalone` run IMAGE_ORIGIN alone.
  fed_images="${IMAGE_ORIGIN}=origin"
  if [ "${fed_topology}" = full ]; then
    fed_images="${fed_images} ${IMAGE_CACHE}=cache ${IMAGE_DIRECTOR}=director"
    fed_images="${fed_images} ${IMAGE_REGISTRY}=registry"
  fi
  fed_version=${PELICAN_TAG#v}
  for fed_image in ${fed_images}; do
    if [ "${fed_image%=*}" != "${IMAGE_HUB}/${fed_image##*=}:${PELICAN_TAG}" ]; then
      fed_version=custom
    fi
  done
  fed_write_value pelican-version "${fed_version}"

  # One line per exported namespace: its prefix and capabilities. The
  # tests read it to know what each namespace should allow.
  for ns in ${fed_namespaces}; do
    fed_namespace_caps "${ns}"
    printf '/%s %s\n' "${ns}" "$(printf '%s' "${fed_caps}" | tr ' ' ,)"
  done | fed_write framework/var/generated/exports

  # One line per running origin that exports these namespaces (so not
  # origin-2, another owner, which is in owners below): service, the URL
  # under which it serves its exports, and its store, the directory
  # (under framework/var) that holds each export's (see fed_storage_dir).
  # A pstore origin's store is encrypted, so the directories there are
  # the plain copy that the transfers suite uploads to it (marked
  # `pstore`).
  {
    if [ "${fed_topology}" = tiny ]; then
      fed_origin_line fed 0
    else
      fed_origin_line origin-0 0
      if fed_has_profile multi-origin; then
        fed_origin_line origin-1 1
      fi
    fi
  } | fed_write framework/var/generated/origins

  # Each running origin of another owner (`topo-multi-owner`): service,
  # data URL, and store, as in origins; the directory (under
  # framework/var) of its issuer keys; and its web URL. owner-exports has
  # one line per export of theirs: service, federation prefix, the
  # namespace it mirrors (`-` for none; see fed_owner_exports), its
  # capabilities, and the issuer URL it gives the prefix, or would give it
  # (/public/other has none, unless /public takes writes). origin-2 always
  # runs its own issuer (see docker-compose.yaml). Both are empty without
  # another owner.
  {
    if fed_has_profile multi-owner; then
      printf '%s %s %s\n' "$(fed_origin_line origin-2 2)" issuer-keys/origin-2 \
          https://origin-2:8444
    fi
  } | fed_write framework/var/generated/owners
  {
    if fed_has_profile multi-owner; then
      fed_owner_exports | while read -r prefix like; do
        fed_namespace_caps "$(fed_owner_caps_of "${like}")"
        printf 'origin-2 %s %s %s %s\n' "${prefix}" "${like}" \
            "$(printf '%s' "${fed_caps}" | tr ' ' ,)" \
            "$(fed_own_issuer_url https://origin-2:8444 "${prefix}")"
      done
    fi
  } | fed_write framework/var/generated/owner-exports

  # Likewise for each running cache: service, data URL, kind, and role.
  # A standalone origin has no cache, nor has the full topology under
  # `topo-no-cache`.
  {
    if [ "${fed_topology}" = tiny ]; then
      fed_cache_line fed
    elif [ "${fed_topology}" = full ]; then
      if [ "${FEDERATED_CACHE}" = true ]; then
        fed_cache_line cache-0
      fi
      if fed_has_profile multi-cache; then
        fed_cache_line cache-1
      fi
      if fed_has_profile site-local-cache; then
        fed_cache_line cache-2 site-local
      fi
    fi
  } | fed_write framework/var/generated/caches

  # Every service's generated layer; see fed_instance_write.
  for svc in ${fed_services} dev; do
    mkdir -p "framework/var/generated/instance/${svc}"
  done

  # The origins' exports (see fed_origin_exports). IssuerUrls is pinned
  # where the origins' own issuer must not be:
  #
  #   topo-multi-origin     two origins export the same prefixes, and must
  #                         advertise one issuer, origin-0's. Only its
  #                         issuer publishes the IssuerJwks keys where
  #                         anyone looks, so origin-1 warns that they
  #                         authorize nothing.
  #   auth-external-issuer  the exports trust fed_external_issuer, which
  #                         has no IssuerJwks of its own.
  pin_issuers=false
  if [ "${fed_topology}" != tiny ] && fed_has_profile multi-origin; then
    pin_issuers=true
  fi
  if [ "${EXTERNAL_ISSUER}" = true ]; then
    pin_issuers=true
  fi
  fed_origin_exports "" "${pin_issuers}" \
      | fed_write framework/var/generated/conf/60-origin-exports.yaml
  # Under `origin-s3`, each origin's exports name buckets of its own, in
  # its layer, which beats the shared one.
  for svc in origin-0 origin-1; do
    if [ "${ORIGIN_VARIANT}" = s3 ]; then
      fed_origin_exports "${svc}" "${pin_issuers}" \
          | fed_instance_write "${svc}" 60-origin-exports.yaml
    else
      fed_instance_rm "${svc}" 60-origin-exports.yaml
    fi
  done

  # origin-2's exports (`topo-multi-owner`; see fed_owner_exports), in its
  # own layer, which beats the shared one.
  if fed_has_profile multi-owner; then
    {
      fed_exports_header
      first=yes
      fed_owner_exports | while read -r prefix like; do
        [ -n "${first}" ] || printf '\n'
        first=""
        bucket=""
        if [ "${ORIGIN_VARIANT}" = s3 ]; then
          bucket=$(fed_s3_bucket origin-2 "${prefix}")
        fi
        fed_export_entry "${prefix}" "$(fed_owner_caps_of "${like}")" \
            "$(fed_storage_prefix "${prefix}")" "" "" "${bucket}"
      done
    } | fed_instance_write origin-2 60-origin-exports.yaml
  else
    fed_instance_rm origin-2 60-origin-exports.yaml
  fi

  # The registry: so that origin-2 can register a prefix inside origin-0's
  # with its own key, it does not require key chaining; and under
  # `registry-approval`, nothing serves until an administrator approves its
  # registration, which the federation suite's `ready` scenario does.
  {
    fed_layer_head
    printf '\nRegistry:\n'
    if fed_has_profile multi-owner; then
      printf '  RequireKeyChaining: false\n'
    fi
    printf '  RequireOriginApproval: %s\n' "${REGISTRY_REQUIRE_APPROVAL}"
    printf '  RequireCacheApproval: %s\n' "${REGISTRY_REQUIRE_APPROVAL}"
  } | fed_write framework/var/generated/conf/60-registry.yaml

  # The directors: where a GET of a bare path goes
  # (`director-default-origin`).
  {
    fed_layer_head
    printf '\nDirector:\n  DefaultResponse: %s\n' "${DIRECTOR_DEFAULT_RESPONSE}"
  } | fed_write framework/var/generated/conf/60-director.yaml

  # The directors that serve the federation's metadata
  # (`director-discovery`), which framework/config.d/director/default/
  # turns off: director-0 alone, so that another director must still
  # answer 410 there; but under `director-ha`, every director, since any
  # may answer at the shared name.
  for svc in ${fed_group_directors}; do
    if [ "${DIRECTOR_DISCOVERY}" = true ] \
        && { [ "${svc}" = director-0 ] || [ "${DIRECTOR_HA}" = true ]; }; then
      {
        fed_layer_head
        printf '\nDirector:\n  EnableFederationMetadataHosting: true\n'
      } | fed_instance_write "${svc}" 60-discovery.yaml
    else
      fed_instance_rm "${svc}" 60-discovery.yaml
    fi
  done

  # The load balancer in front of the directors (`director-ha`): each
  # director's external URL is the shared name, which the tokens its web
  # API takes must name; its advertise URL, where the servers reach it,
  # is its own; and so is its name, which Pelican would otherwise take
  # from the external URL (GetServerMetadata in server_utils/sitename.go),
  # giving both directors one. generated/director-ha tells the tests.
  for svc in ${fed_group_directors}; do
    if [ "${DIRECTOR_HA}" = true ]; then
      {
        fed_layer_head
        printf '\nServer:\n  ExternalWebUrl: %s\n\n' "${fed_director_url}"
        printf 'Director:\n  AdvertiseUrl: https://%s:8444\n\n' "${svc}"
        printf 'Xrootd:\n  Sitename: %s\n' "${svc}"
      } | fed_instance_write "${svc}" 60-ha.yaml
    else
      fed_instance_rm "${svc}" 60-ha.yaml
    fi
  done
  fed_write_value director-ha "${DIRECTOR_HA}"

  # How long the origins' embedded issuer's tokens last
  # (`origin-short-tokens`): Pelican's defaults otherwise. issuer-lifetimes
  # tells the tests, in seconds: access tokens, refresh tokens, how long a
  # used refresh token still works, and authorization and device codes.
  # export-templates says whether /protected-b has templates of its own
  # (see fed_export_entry).
  if [ "${ISSUER_SHORT_TOKENS}" = true ]; then
    {
      fed_layer_head
      printf '\nIssuer:\n'
      printf '  AccessTokenLifetime: 60s\n'
      printf '  RefreshTokenLifetime: 240s\n'
      printf '  RefreshTokenGracePeriod: 20s\n'
      printf '  AuthorizationCodeLifetime: 60s\n'
    } | fed_write framework/var/generated/conf/60-issuer.yaml
    printf '60 240 20 60\n'
  else
    rm -f framework/var/generated/conf/60-issuer.yaml
    printf '3600 604800 300 600\n'
  fi | fed_write framework/var/generated/issuer-lifetimes
  fed_write_value export-templates "${ORIGIN_EXPORT_TEMPLATES}"

  # Each origin's backend (`origin-https`, `origin-httpsv2`, `origin-s3`,
  # `origin-s3v2`): its store, framework/var/data/origin/<N>, is
  # /srv/origin-<N> on the backend, served by the S3 server as bucket
  # origin-<N>, in which each export's StoragePrefix leads to its
  # directory. An s3 origin's exports name buckets of their own instead
  # (fed_s3_bucket).
  for svc in origin-0 origin-1 origin-2; do
    case "${ORIGIN_VARIANT}" in
      https|httpsv2)
          {
            fed_layer_head
            printf '\nOrigin:\n  HttpServiceUrl: https://webdav:8444/%s\n' "${svc}"
          } | fed_instance_write "${svc}" 60-backend.yaml ;;
      s3|s3v2)
          {
            fed_layer_head
            printf '\nOrigin:\n  S3Bucket: %s\n' "${svc}"
          } | fed_instance_write "${svc}" 60-backend.yaml ;;
      *)  fed_instance_rm "${svc}" 60-backend.yaml ;;
    esac
  done

  # Multiuser (`origin-multiuser`): each request's filesystem operations
  # run as the Unix user its token maps to, who must exist in the
  # origin's image. The tests' tokens map to `pelican` (uid 10941); any
  # other token, e.g. a director's test, to `xrootd` (10940); and
  # requests without a token run as `nobody`, Pelican's default. The
  # rules after the first are the `users` suite's, for subjects and a
  # group that only its tokens carry: two map to system accounts, which
  # Origin.MultiuserMinID must refuse; the rest map to `pelican`, so that
  # an upload's owner shows that the rule matched, not the default. A
  # `path` rule is relative to the namespace, as in XrdSciTokens' mapfile
  # format, which Origin.ScitokensNameMapFile follows.
  if [ "${ORIGIN_MULTIUSER}" = true ]; then
    {
      printf '[\n'
      printf '  {"sub": "pelican-test-framework", "result": "pelican",\n'
      printf '   "comment": "the tests'"'"' tokens (framework/testlib/credentials.py)"},\n'
      printf '  {"sub": "users-root", "result": "root",\n'
      printf '   "comment": "the users suite'"'"'s min-id: below Origin.MultiuserMinID"},\n'
      printf '  {"sub": "users-daemon", "result": "daemon",\n'
      printf '   "comment": "the users suite'"'"'s min-id: below Origin.MultiuserMinID"},\n'
      printf '  {"sub": "users-path", "path": "/data/users/mapped", "result": "pelican",\n'
      printf '   "comment": "the users suite'"'"'s mapfile path"},\n'
      printf '  {"group": "xrootd", "result": "pelican",\n'
      printf '   "comment": "the users suite'"'"'s mapfile group"}\n'
      printf ']\n'
    } | fed_write framework/var/generated/multiuser-mapfile.json
    {
      fed_layer_head
      printf '\nOrigin:\n'
      printf '  ScitokensNameMapFile: /fed/generated/multiuser-mapfile.json\n'
      printf '  ScitokensDefaultUser: xrootd\n'
    } | fed_write framework/var/generated/conf/60-multiuser.yaml
  else
    rm -f framework/var/generated/multiuser-mapfile.json \
        framework/var/generated/conf/60-multiuser.yaml
  fi

  # XRootD's POSC (`origin-atomic-uploads`) stages each upload in
  # Origin.UploadTempLocation, which Pelican requires to be on the exports'
  # filesystem but inside none of them: a directory beside their storage
  # in each origin's store, which the origin creates.
  if [ "${ORIGIN_ATOMIC_UPLOADS}" = true ]; then
    {
      fed_layer_head
      printf '\nOrigin:\n  UploadTempLocation: /data/.in-progress\n'
    } | fed_write framework/var/generated/conf/60-atomic-uploads.yaml
  else
    rm -f framework/var/generated/conf/60-atomic-uploads.yaml
  fi

  # The ssh origin's tunnel (`origin-ssh-tunnel`): the origin dials its
  # helper through the SSH connection, instead of the helper connecting
  # back to the origin's web URL.
  if [ "${ORIGIN_SSH_TUNNEL}" = true ]; then
    {
      fed_layer_head
      printf '\nOrigin:\n  SSH:\n    TunnelCallback: true\n'
    } | fed_write framework/var/generated/conf/60-ssh-tunnel.yaml
  else
    rm -f framework/var/generated/conf/60-ssh-tunnel.yaml
  fi

  # The connection broker (`origin-broker`): origin-0 advertises the
  # broker URL and polls it, and the caches refrain from advertising one
  # of their own, which the director would broker to as well. A director
  # signs each brokered connection's certificate with the framework's CA,
  # so it gets a copy of the CA's key, once `init` has made one.
  if [ "${ORIGIN_BROKER}" = true ]; then
    {
      fed_layer_head
      printf '\nOrigin:\n  EnableBroker: true\n\n'
      printf 'Cache:\n  EnableBroker: false\n'
    } | fed_write framework/var/generated/conf/60-broker.yaml
    for svc in ${fed_group_directors}; do
      if [ -f framework/var/certs/ca.key ]; then
        fed_instance_write "${svc}" ca.key <framework/var/certs/ca.key
      fi
      {
        fed_layer_head
        printf '\nServer:\n  TLSCAKey: /fed/generated.instance/ca.key\n'
      } | fed_instance_write "${svc}" 60-broker.yaml
    done
  else
    rm -f framework/var/generated/conf/60-broker.yaml
    for svc in ${fed_group_directors}; do
      fed_instance_rm "${svc}" ca.key
      fed_instance_rm "${svc}" 60-broker.yaml
    done
  fi

  # Cache tiering (`cache-tiered`): each V2 cache tiers objects of at least
  # 16 MiB, more than any other suite reads, to a prefix of its own in the
  # `s3` service's bucket `tier`, framework/var/data/tier. cache-1
  # (`topo-multi-cache`) proxies tiered objects; the others redirect to
  # them. generated/tiering has a line per cache: service, mode,
  # threshold in bytes, and its prefix's directory under framework/var.
  {
    if [ "${CACHE_TIERING}" = true ]; then
      while read -r svc _; do
        mode=redirect
        if [ "${svc}" = cache-1 ]; then
          mode=proxy
        fi
        printf '%s %s %s data/tier/%s\n' "${svc}" "${mode}" 16777216 "${svc}"
      done <framework/var/generated/caches
    fi
  } | fed_write framework/var/generated/tiering
  for svc in cache-0 cache-1 cache-2; do
    if [ "${CACHE_TIERING}" = true ]; then
      disable_redirect=false
      if [ "${svc}" = cache-1 ]; then
        disable_redirect=true
      fi
      {
        fed_layer_head
        printf '\nCache:\n'
        printf '  TieringThreshold: 16MB\n'
        printf '  TieringRedirectExpiry: 30s\n'
        printf '  TieringDisableRedirect: %s\n' "${disable_redirect}"
        printf '  TieringTargets:\n'
        printf '    - ServiceUrl: https://s3:8444\n'
        printf '      Region: us-east-1\n'
        printf '      UrlStyle: path\n'
        printf '      Bucket: tier\n'
        printf '      Prefix: %s\n' "${svc}"
        printf '      MaxSize: 2GB\n'
        printf '      AccessKeyfile: /fed/generated/s3-access-key\n'
        printf '      SecretKeyfile: /fed/generated/s3-secret-key\n'
      } | fed_instance_write "${svc}" 60-tiering.yaml
    else
      fed_instance_rm "${svc}" 60-tiering.yaml
    fi
  done

  # The V2 caches' memory cache (`cache-memory`), which only the cache
  # module reads.
  if [ -n "${CACHE_MEMORY_SIZE}" ]; then
    {
      fed_layer_head
      printf '\nCache:\n'
      printf '  MemoryCacheSize: %s\n' "${CACHE_MEMORY_SIZE}"
    } | fed_write framework/var/generated/conf/60-cache-store.yaml
  else
    rm -f framework/var/generated/conf/60-cache-store.yaml
  fi

  # The bounded store (`cache-small`), in each cache's own layer, since
  # the kinds of cache take different settings, and every server checks
  # the water marks it is given. A V2 cache takes the water marks. An
  # XRootD cache's water marks are of the whole filesystem under its
  # store, so it bounds its own files instead
  # (Cache.Files{Base,Nominal,Max}Size, XRootD's `pfc.diskusage ...
  # files`), from the low mark to the high one, beside the water marks
  # that Pelican then requires, at Pelican's defaults. Sizes end in `m`
  # (MiB), which Pelican's check, the V2 cache, and XRootD all take; the
  # check refuses `MB`, and XRootD reads a bare number as a share of the
  # disk. generated/eviction has a line per bounded cache: service, and
  # its high and low marks in bytes.
  for svc in fed cache-0 cache-1 cache-2; do
    kind=$(awk -v svc="${svc}" '$1 == svc { print $3 }' framework/var/generated/caches)
    if [ -z "${CACHE_HIGH_WATER_MIB}" ] || [ -z "${kind}" ]; then
      fed_instance_rm "${svc}" 60-cache-bound.yaml
      continue
    fi
    {
      fed_layer_head
      printf '\nCache:\n'
      if [ "${kind}" = xrootd ]; then
        printf '  HighWaterMark: 89\n'
        printf '  LowWaterMark: 85\n'
        printf '  FilesBaseSize: %sm\n' "${CACHE_LOW_WATER_MIB}"
        printf '  FilesNominalSize: %sm\n' \
            "$(( (CACHE_LOW_WATER_MIB + CACHE_HIGH_WATER_MIB) / 2 ))"
        printf '  FilesMaxSize: %sm\n' "${CACHE_HIGH_WATER_MIB}"
      else
        printf '  HighWaterMark: %sm\n' "${CACHE_HIGH_WATER_MIB}"
        printf '  LowWaterMark: %sm\n' "${CACHE_LOW_WATER_MIB}"
      fi
    } | fed_instance_write "${svc}" 60-cache-bound.yaml
  done
  {
    if [ -n "${CACHE_HIGH_WATER_MIB}" ]; then
      while read -r svc _; do
        printf '%s %s %s\n' "${svc}" "$(( CACHE_HIGH_WATER_MIB * 1048576 ))" \
            "$(( CACHE_LOW_WATER_MIB * 1048576 ))"
      done <framework/var/generated/caches
    fi
  } | fed_write framework/var/generated/eviction

  # The external issuer (`auth-external-issuer`), which the discovery host
  # serves from here; see framework/etc/nginx-discovery.conf. Without it,
  # the discovery host answers 404.
  mkdir -p framework/var/generated/issuer
  if [ "${EXTERNAL_ISSUER}" = true ]; then
    {
      printf '{\n'
      printf '  "issuer": "%s",\n' "${fed_external_issuer}"
      printf '  "jwks_uri": "%s/.well-known/issuer.jwks"\n' "${fed_external_issuer}"
      printf '}\n'
    } | fed_write framework/var/generated/issuer/openid-configuration.json
    fed_write_issuer_jwks
  else
    rm -f framework/var/generated/issuer/openid-configuration.json \
        framework/var/generated/issuer/issuer.jwks
  fi

  # The lab server's bind mount needs a source. `init` writes the real
  # one; with the lab server running, fed_require_lab_key insists on it.
  mkdir -p framework/var/generated/ssh
  if ! fed_has_profile lab && [ ! -e framework/var/generated/ssh/authorized_keys ]; then
    : >framework/var/generated/ssh/authorized_keys
  fi

  # Local Pelican configuration is copied, not mounted, so that the
  # framework never writes to local/. Pelican requires every directory in
  # ConfigLocations, so each copy exists even when empty.
  fed_copy_dir local/config.d/base framework/var/generated/local/base
  for svc in ${fed_services} dev; do
    fed_copy_dir "local/config.d/instance/${svc}" \
        "framework/var/generated/local/${svc}"
  done

  # The S3 server's credentials (see framework/presets/origin-s3v2.sh).
  for f in s3-access-key s3-secret-key; do
    if [ -f "local/etc/${f}" ]; then
      fed_write "framework/var/generated/${f}" <"local/etc/${f}"
    else
      fed_write "framework/var/generated/${f}" <"framework/etc/${f}"
    fi
  done
  # The OIDC client that every server is (`auth-oidc-local`,
  # `auth-oidc-cilogon`): yours, from local/etc/, for CILogon or a
  # provider of your own; or else one made at random, once, which the
  # identity provider takes as its one client.
  for f in oidc-client-id oidc-client-secret; do
    if [ -f "local/etc/${f}" ]; then
      fed_write "framework/var/generated/${f}" <"local/etc/${f}"
    elif [ ! -s "framework/var/generated/${f}" ]; then
      openssl rand -hex 16 | fed_write "framework/var/generated/${f}"
    fi
  done
  # The identity provider's users (`-p auth-oidc-local`): framework/etc/'s,
  # and any in local/etc/, which replace those of the same username.
  # Written in place, so that the provider, which rereads the file when
  # it changes, sees users added while it runs.
  if [ -f local/etc/idp-users.json ]; then
    fed_require_python
    fed_idp_users=$("${fed_python}" - framework/etc/idp-users.json local/etc/idp-users.json <<'EOF'
import json, sys
users = {}
for path in sys.argv[1:]:
    try:
        with open(path) as f:
            listed = json.load(f)
    except ValueError as e:
        sys.exit(f"{path}: {e}")
    if not isinstance(listed, dict):
        sys.exit(f"{path}: not a JSON object of users, by username")
    users.update(listed)
json.dump(users, sys.stdout, indent=2)
EOF
    ) || die "cannot merge local/etc/idp-users.json"
    printf '%s\n' "${fed_idp_users}" | fed_write framework/var/generated/idp-users.json
  else
    fed_write framework/var/generated/idp-users.json <framework/etc/idp-users.json
  fi

  # The static token with which the exports suite has an https or httpsv2
  # origin reach its backend (Origin.HttpAuthTokenFile, at
  # /fed/generated/http-auth-token in the origin), through the
  # `webdav-gate` service, which records what the origin sends. Made
  # once, at random: the backend checks nothing.
  if [ ! -s framework/var/generated/http-auth-token ]; then
    openssl rand -hex 24 | fed_write framework/var/generated/http-auth-token
  fi
}

# Create every bind-mount source, so that Docker does not create them as
# root.
fed_prepare_dirs() {
  for svc in ${fed_services}; do
    mkdir -p "framework/var/state/${svc}" "framework/var/issuer-keys/${svc}"
  done
  for svc in ${fed_key_others}; do
    mkdir -p "framework/var/issuer-keys/${svc}"
  done
  # Each service's last configuration layer, which the tests write (see
  # framework/testlib/control.py).
  for svc in ${fed_services} dev; do
    mkdir -p "framework/var/control/config/${svc}"
  done
  mkdir -p framework/var/control/requests
  for store in ${fed_stores} lab-server metadata tier webdav-gate idp; do
    mkdir -p "framework/var/data/${store}"
  done
  mkdir -p framework/var/grafana framework/var/issuer-jwks framework/var/test-keys
  # What the dev container keeps for itself (see docker-compose.yaml), and
  # the host's binaries, which it cannot write.
  mkdir -p framework/var/results framework/var/dev framework/var/bin/linux \
      framework/var/bin/host
  # Written over SSH by `alice`, whose uid is unrelated to yours.
  chmod 0777 framework/var/data/lab-server
  # Each export's storage (fed_storage_dir), which the s3 service mounts
  # as a bucket under `origin-s3`, and which an origin writes in as
  # whatever user it runs as.
  for store in framework/var/data/origin/0 framework/var/data/origin/1 \
      framework/var/data/lab-server; do
    fed_prepare_storage "${store}" public protected-a protected-b
  done
  fed_prepare_storage framework/var/data/origin/2 other public-other public-secret
}

# Make directories $2... in store $1, writable by any user, unless they
# are there. The containers write the store, so one that is a symlink
# is left alone rather than followed.
fed_prepare_storage() {
  store=$1
  shift
  for dir in "$@"; do
    if [ ! -d "${store}/${dir}" ] && [ ! -L "${store}/${dir}" ]; then
      mkdir "${store}/${dir}"
      chmod 0777 "${store}/${dir}"
    fi
  done
}

# Services read their configuration only at startup. Mark a change to
# framework/var/generated/conf/ or instance/ so that `up` can name the
# services still running the old configuration.
fed_conf_sum() {
  cat framework/var/generated/conf/*.yaml framework/var/generated/instance/*/*.yaml \
      2>/dev/null | cksum
}

fed_conf_was=$(fed_conf_sum)
fed_generate
if [ "$(fed_conf_sum)" != "${fed_conf_was}" ]; then
  : >framework/var/generated/conf-changed
fi
fed_prepare_dirs

#---------------------------------------------------------------------------
# PELICAN_TAG's binaries in framework/var/bin/, for use outside the
# service containers:
#
#   linux/    the client the tests use in the dev container, and
#             pelican-server for the server-only commands they run; the
#             dev container may write them (your own client, README.md)
#   host/     pelican-server for `keys`, plus a client, for the host
#
# The host runs only those in host/, which no container can write, so
# that nothing in a container can change what the host runs (see the dev
# service in docker-compose.yaml). On a Linux host, the two directories
# start with the same binaries.

fed_os=$(uname -s)
fed_arch=$(uname -m)
case "${fed_arch}" in
  aarch64) fed_arch=arm64 ;;
esac
fed_host_bin=framework/var/bin/host

# Copy file $1 to $2, in a directory that a container may write, following
# no symlink at either (see framework/put-file.py).
fed_put() {
  fed_require_python
  "${fed_python}" framework/put-file.py "$1" "$2" || exit 1
}

# Download binary $1 at version $2 for OS $3 as $4/$1-$3, in directory $4,
# which no container can write.
fed_fetch_archive() {
  tarball="$1_$3_${fed_arch}.tar.gz"
  ( cd "$4" \
      && curl -fSL -O \
          "https://github.com/PelicanPlatform/pelican/releases/download/v$2/${tarball}" \
      && tar xzf "${tarball}" \
      && mv "$1-$2/$1" "$1-$3" \
      && rm -rf "${tarball}" "$1-$2" ) \
      || die "cannot download $1 $2 for $3"
}

# True if framework/var/bin/ holds PELICAN_TAG's binaries. Only the
# host's pelican-server is version-checked, so a custom linux/pelican or
# linux/pelican-server survives `init`.
fed_have_binaries() {
  [ -x framework/var/bin/linux/pelican ] || return 1
  [ -e framework/var/bin/linux/stash_plugin ] || return 1
  [ -x "${fed_host_bin}/pelican" ] || return 1
  [ -x "${fed_host_bin}/pelican-server" ] || return 1
  "${fed_host_bin}/pelican-server" --version 2>/dev/null \
      | grep -qxF "Version: ${PELICAN_TAG#v}" || return 1
  return 0
}

# Download PELICAN_TAG's binaries (see above): all of them, or, given
# `linux-server`, linux/pelican-server alone. Each is downloaded into a
# directory of framework/var/bin/, which no container can write, and then
# copied into place.
fed_fetch_binaries() {
  version=${PELICAN_TAG#v}
  fed_stage=$(mktemp -d framework/var/bin/.fetch.XXXXXX)
  if [ "${1:-}" = linux-server ]; then
    fed_fetch_archive pelican-server "${version}" Linux "${fed_stage}"
    fed_put "${fed_stage}/pelican-server-Linux" framework/var/bin/linux/pelican-server
    rm -rf "${fed_stage}"
    return 0
  fi

  printf 'Downloading the %s binaries ...\n' "${version}"
  for name in pelican pelican-server; do
    fed_fetch_archive "${name}" "${version}" Linux "${fed_stage}"
    if [ "${fed_os}" != Linux ]; then
      fed_fetch_archive "${name}" "${version}" "${fed_os}" "${fed_stage}"
    fi
    fed_put "${fed_stage}/${name}-Linux" "framework/var/bin/linux/${name}"
    fed_put "${fed_stage}/${name}-${fed_os}" "${fed_host_bin}/${name}"
  done
  rm -rf "${fed_stage}"

  # The client dispatches on its name; the transfers suite's plugin-*
  # scenarios invoke stash_plugin. With -n, a symlink already there is
  # replaced, not followed.
  ln -sfn pelican framework/var/bin/linux/stash_plugin
  ln -sfn pelican "${fed_host_bin}/stash_plugin"
}

#---------------------------------------------------------------------------
# Issuer keys.
#
# Keys are named NN-<stamp>.pem. Pelican signs with the one that sorts
# first, so NN alone decides which key is active:
#
#   keys init     NN=50
#   keys add      NN above every existing key (verify-only)
#   keys rotate   NN below every existing key (becomes the signer)
#
# Pelican ignores the .jwks beside each key. `keys list` reads it, and
# the external issuer (`auth-external-issuer`) publishes its own.

# Die unless $1 is a target of `keys add` or `keys rotate`.
fed_key_check_target() {
  for target in ${fed_services} ${fed_key_others} ${fed_groups} all; do
    if [ "${target}" = "$1" ]; then
      return 0
    fi
  done
  die_usage "no such key target: $1" \
      "(try: ${fed_services} ${fed_key_others} ${fed_groups} all)"
}

# True if service $1 is in a group (see fed_groups).
fed_key_grouped() {
  case " ${fed_group_origins} ${fed_group_directors} " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# The directories that must hold the same key as keyset $1. Used in
# command substitutions, so it must not die.
# shellcheck disable=SC2086  # the lists are meant to be split
fed_key_dirs() {
  case "$1" in
    origins)   printf '%s ' ${fed_group_origins} ;;
    directors) printf '%s ' ${fed_group_directors} ;;
    *)         printf '%s ' "$1" ;;
  esac
}

# The keysets that target $1 names. `all` is one key for each group
# plus one per other key holder, not one key for everything.
fed_key_sets() {
  case "$1" in
    all) printf '%s' "${fed_groups}"
         for svc in ${fed_services} ${fed_key_others}; do
           if ! fed_key_grouped "${svc}"; then
             printf ' %s' "${svc}"
           fi
         done
         printf '\n' ;;
    *)   printf '%s\n' "$1" ;;
  esac
}

# The NN of each key in issuer-key directories $@, one per line.
fed_key_prefixes() {
  for dir in "$@"; do
    for key in "framework/var/issuer-keys/${dir}"/[0-9][0-9]-*.pem; do
      [ -e "${key}" ] || continue
      key=$(basename -- "${key}")
      printf '%s\n' "${key%%-*}"
    done
  done
}

# Die unless the host's pelican-server, which alone has `key create`, is
# there.
fed_require_pelican_server() {
  [ -x "${fed_host_bin}/pelican-server" ] \
      || die "${fed_host_bin}/pelican-server is missing;" \
             "run './${progname} init' first"
}

# Create key $1 in the first issuer-key directory of $2... and copy it to
# the rest.
fed_key_make() {
  name=$1; shift
  first=$1

  fed_require_pelican_server
  for dir in "$@"; do
    [ -w "framework/var/issuer-keys/${dir}" ] \
        || die "framework/var/issuer-keys/${dir}/ is not yours to write: under" \
               "SERVER_DROP_PRIVILEGES, Pelican gives it to the container's" \
               "pelican user. Start over with ./reset.sh"
  done

  # The containers write the issuer-key directories, so the key is made
  # in framework/var/, which none can write, and copied into each with
  # fed_put, which follows no symlink they may have left there.
  printf 'Creating framework/var/issuer-keys/%s/%s.pem ...\n' "${first}" "${name}"
  fed_stage=$(mktemp -d framework/var/.keys.XXXXXX)
  "${fed_host_bin}/pelican-server" key create \
      --private-key "${fed_stage}/${name}.pem" \
      --public-key "${fed_stage}/${name}.jwks" >/dev/null
  [ -f "${fed_stage}/${name}.pem" ] && [ -f "${fed_stage}/${name}.jwks" ] \
      || die "framework/var/issuer-keys/${first}/${name}.pem was not created"
  chmod 0640 "${fed_stage}/${name}.pem"
  chmod 0644 "${fed_stage}/${name}.jwks"

  for dir in "$@"; do
    if [ "${dir}" != "${first}" ]; then
      printf 'Copying it to framework/var/issuer-keys/%s/ ...\n' "${dir}"
    fi
    fed_put "${fed_stage}/${name}.pem" "framework/var/issuer-keys/${dir}/${name}.pem"
    fed_put "${fed_stage}/${name}.jwks" "framework/var/issuer-keys/${dir}/${name}.jwks"
  done
  rm -rf "${fed_stage}"
}

# True if directory $1 holds an NN-*.pem. Keys Pelican generated for
# itself (pelican_generated_*.pem) do not count: Pelican creates one in
# an empty directory, which would silently split a group. A directory you
# cannot list counts as holding one: under SERVER_DROP_PRIVILEGES,
# Pelican gives it to the container's pelican user, which on a Linux host
# shuts you out, but only after it held a key.
fed_key_dir_has_key() {
  if [ -d "framework/var/issuer-keys/$1" ] \
      && { [ ! -r "framework/var/issuer-keys/$1" ] || [ ! -x "framework/var/issuer-keys/$1" ]; }; then
    return 0
  fi
  for key in "framework/var/issuer-keys/$1"/[0-9][0-9]-*.pem; do
    if [ -e "${key}" ]; then
      return 0
    fi
  done
  return 1
}

# Give every member of group $1 a key: create one if no member has any,
# otherwise copy from a member that does. A member holding only some of
# the group's keys (after `keys rotate <member>`) is left alone.
fed_key_init_group() {
  source_dir=""
  for dir in $(fed_key_dirs "$1"); do
    if [ -z "${source_dir}" ] && fed_key_dir_has_key "${dir}"; then
      source_dir=${dir}
    fi
  done

  if [ -z "${source_dir}" ]; then
    # shellcheck disable=SC2046
    fed_key_make 50-initial $(fed_key_dirs "$1")
    return 0
  fi

  for dir in $(fed_key_dirs "$1"); do
    if fed_key_dir_has_key "${dir}"; then
      continue
    fi
    printf 'Copying framework/var/issuer-keys/%s/ to framework/var/issuer-keys/%s/ ...\n' \
        "${source_dir}" "${dir}"
    for key in "framework/var/issuer-keys/${source_dir}"/[0-9][0-9]-*.pem \
               "framework/var/issuer-keys/${source_dir}"/[0-9][0-9]-*.jwks; do
      [ -e "${key}" ] || [ -L "${key}" ] || continue
      fed_put "${key}" "framework/var/issuer-keys/${dir}/${key##*/}"
    done
  done
}

# Every service gets a key, whether or not the current shape starts it.
fed_key_init() {
  for group in ${fed_groups}; do
    fed_key_init_group "${group}"
  done

  for svc in ${fed_services} ${fed_key_others}; do
    if fed_key_grouped "${svc}"; then
      continue
    fi
    if ! fed_key_dir_has_key "${svc}"; then
      fed_key_make 50-initial "${svc}"
    fi
  done

  fed_test_key_make framework/var/test-keys/jwks.pem \
      framework/var/issuer-jwks/test.jwks
  fed_test_key_make framework/var/test-keys/unknown.pem \
      framework/var/test-keys/unknown.jwks
  for ns in protected-a protected-b; do
    fed_test_key_make "framework/var/test-keys/ns-${ns}.pem" \
        "framework/var/issuer-jwks/ns-${ns}.jwks"
  done

  fed_key_publish
}

# Publish the external issuer's keys at once, since the tests sign with
# its newest as soon as it exists.
fed_key_publish() {
  if [ "${EXTERNAL_ISSUER}" = true ]; then
    fed_write_issuer_jwks
  fi
}

# Create test key $1 and its public key (JWKS) $2, unless both exist.
# The test keys are for the credentials in
# framework/testlib/credentials.py, and like the issuer keys, are created
# once and never replaced:
#
#   test-keys/jwks.pem        its public key is issuer-jwks/test.jwks,
#                             which the origins name in Server.IssuerJwks
#   test-keys/unknown.pem     its public key (unknown.jwks) goes nowhere
#   test-keys/ns-<ns>.pem     its public key is issuer-jwks/ns-<ns>.jwks,
#                             which the origins name in the IssuerJwks of
#                             the /<ns> export alone; only /protected-a
#                             and /protected-b have keys of their own
#
# The origins mount issuer-jwks/ but not test-keys/, so no server holds
# any of the private keys.
fed_test_key_make() {
  if [ -f "$1" ] && [ -f "$2" ]; then
    return 0
  fi
  fed_require_pelican_server

  # `key create` refuses to overwrite a public key, and derives a missing
  # one from an existing private key.
  rm -f "$2"
  printf 'Creating %s ...\n' "$1"
  "${fed_host_bin}/pelican-server" key create \
      --private-key "$1" --public-key "$2" >/dev/null
  [ -f "$1" ] && [ -f "$2" ] || die "$1 was not created"
  chmod 0640 "$1"
  chmod 0644 "$2"
}

# Add a key to keyset $2 for verb $1 (add or rotate), named with stamp $3.
fed_key_new_set() {
  verb=$1
  target=$2
  stamp=$3

  # shellcheck disable=SC2046
  set -- $(fed_key_dirs "${target}")
  used=$(fed_key_prefixes "$@")
  [ -n "${used}" ] \
      || die "${target} has no NN-*.pem key; run './${progname} keys init' first"

  # ${next#0}: a leading zero would make the shell read the number as
  # octal.
  case "${verb}" in
    add)
      next=$(printf '%s\n' "${used}" | sort -n | tail -1)
      next=$(( ${next#0} + 1 ))
      [ "${next}" -le 99 ] \
          || die "no room above prefix 99 in ${target};" \
                 "prune old keys or ./reset.sh"
      ;;
    rotate)
      next=$(printf '%s\n' "${used}" | sort -n | head -1)
      next=$(( ${next#0} - 1 ))
      [ "${next}" -ge 0 ] \
          || die "no room below prefix 00 in ${target};" \
                 "prune old keys or ./reset.sh"
      ;;
    *) die "unknown key verb: ${verb}" ;;
  esac

  fed_key_make "$(printf '%02d-%s' "${next}" "${stamp}")" "$@"
}

# Add a key to each keyset that target $2 names, for verb $1 (add or
# rotate).
fed_key_new() {
  verb=$1
  target=${2:-}
  [ -n "${target}" ] \
      || die_usage "keys ${verb} needs a target" \
                   "(try: origins, directors, issuer, all, or a service name)"
  fed_key_check_target "${target}"

  stamp=$(date +%Y%m%d-%H%M%S)
  for keyset in $(fed_key_sets "${target}"); do
    fed_key_new_set "${verb}" "${keyset}" "${stamp}"
  done
  fed_key_publish

  case "${verb}" in
    add)
      printf '\nAdded a verify-only key. The signing key is unchanged.\n'
      printf 'Running services notice within a minute.\n'
      ;;
    rotate)
      printf '\nThe new key is now the signing key; the old ones still verify.\n'
      printf 'Running services switch to it within a minute, but an origin or\n'
      printf 'cache that already fetched this keyset can deny tokens signed\n'
      printf 'with it for up to 15 minutes more. ./%s restart skips the wait.\n' \
          "${progname}"
      ;;
  esac
}

# The key ID of the first key in JWKS file $1.
fed_jwks_kid() {
  sed -n 's/.*"kid"[^"]*"\([^"]*\)".*/\1/p' "$1" | head -1
}

# Describe every issuer key and test key.
fed_key_list() {
  for svc in ${fed_services} ${fed_key_others}; do
    printf '%s\n' "${svc}"
    active=" (active)"
    found=""
    for key in "framework/var/issuer-keys/${svc}"/*.pem; do
      [ -e "${key}" ] || continue
      found=yes
      kid="no .jwks"
      if [ -f "${key%.pem}.jwks" ]; then
        kid=$(fed_jwks_kid "${key%.pem}.jwks")
      fi
      printf '  %-28s %s%s\n' "$(basename -- "${key}")" "${kid}" "${active}"
      active=""
    done
    [ -n "${found}" ] || printf '  (no keys)\n'
  done
  printf '\nThe first key listed for a service is the one it signs with. The\n'
  printf 'discovery host publishes issuer'"'"'s under auth-external-issuer.\n'

  printf '\nTest keys (framework/var/test-keys/):\n'
  for key in jwks:issuer-jwks/test.jwks unknown:test-keys/unknown.jwks \
             ns-protected-a:issuer-jwks/ns-protected-a.jwks \
             ns-protected-b:issuer-jwks/ns-protected-b.jwks; do
    jwks="framework/var/${key#*:}"
    kid="(missing)"
    if [ -f "framework/var/test-keys/${key%%:*}.pem" ] && [ -f "${jwks}" ]; then
      kid=$(fed_jwks_kid "${jwks}")
    fi
    printf '  %-28s %s\n' "${key%%:*}.pem" "${kid}"
  done
  printf "The origins list jwks.pem's public key in Server.IssuerJwks, and\n"
  printf "ns-<ns>.pem's in the IssuerJwks of the /<ns> export alone;\n"
  printf 'no server knows unknown.pem.\n'
}

# The `keys` command.
fed_keys() {
  case "${1:-list}" in
    list)    fed_key_list ;;
    init)    fed_key_init ;;
    add)     shift; fed_key_new add "$@" ;;
    rotate)  shift; fed_key_new rotate "$@" ;;
    *)       die_usage "keys takes list, init," \
                       "add <target>, or rotate <target>" ;;
  esac
}

#---------------------------------------------------------------------------
# Preflight checks for `up` and `restart`.

# The Pelican services that the shape runs, one per line.
fed_running_services() {
  case "${fed_topology}" in
    tiny)       printf '%s\n' fed; return 0 ;;
    standalone) printf '%s\n' origin-0; return 0 ;;
  esac
  printf '%s\n' director-0 registry origin-0
  if fed_has_profile cache; then
    printf '%s\n' cache-0
  fi
  if fed_has_profile multi-director; then
    printf '%s\n' director-1
  fi
  if fed_has_profile multi-origin; then
    printf '%s\n' origin-1
  fi
  if fed_has_profile multi-owner; then
    printf '%s\n' origin-2
  fi
  if fed_has_profile multi-cache; then
    printf '%s\n' cache-1
  fi
  if fed_has_profile site-local-cache; then
    printf '%s\n' cache-2
  fi
  if fed_has_profile transfer; then
    printf '%s\n' transfer
  fi
}

# True if every certificate exists and none expires within a day.
fed_certs_fresh() {
  for cert in ${fed_certs}; do
    [ -f "${cert}" ] || return 1
    case "${cert}" in
      *.crt) openssl x509 -checkend 86400 -noout -in "${cert}" \
                 >/dev/null 2>&1 || return 1 ;;
    esac
  done
  return 0
}

# True if the server certificate was made from framework/etc/tls.req as
# it is now, which names every host (init-certs.sh keeps a copy).
fed_certs_current() {
  cmp -s framework/etc/tls.req framework/var/certs/tls.req
}

# Die if a certificate is missing; warn if one is stale.
fed_require_certs() {
  for cert in ${fed_certs}; do
    [ -f "${cert}" ] || die "${cert} is missing; run './${progname} init'"
  done
  if ! fed_certs_fresh; then
    warn "framework/var/certs/ has expired or expires within a day;" \
         "replace it with './${progname} init'"
  elif ! fed_certs_current; then
    warn "framework/etc/tls.req has changed since the server certificate was" \
         "issued; reissue it with './${progname} init'"
  fi
}

# Die unless every running service, and the external issuer if used, has
# an issuer key, and every test key's JWKS exists.
fed_require_keys() {
  missing=""
  for svc in $(fed_running_services); do
    if ! fed_key_dir_has_key "${svc}"; then
      missing="${missing} ${svc}"
    fi
  done
  if [ "${EXTERNAL_ISSUER}" = true ] && ! fed_key_dir_has_key issuer; then
    missing="${missing} issuer"
  fi
  [ -z "${missing}" ] \
      || die "no issuer key for:${missing}; run './${progname} keys init'"
  # An origin whose Server.IssuerJwks file is missing does not start. One
  # whose export's IssuerJwks file is missing starts, but serves that
  # namespace only its own keys.
  for jwks in test ns-protected-a ns-protected-b; do
    [ -f "framework/var/issuer-jwks/${jwks}.jwks" ] \
        || die "framework/var/issuer-jwks/${jwks}.jwks is missing;" \
               "run './${progname} keys init'"
  done
}

# Die if the lab server would start without the SSH key that `init` makes.
fed_require_lab_key() {
  fed_has_profile lab || return 0
  [ -s framework/var/generated/ssh/authorized_keys ] \
      || die "framework/var/generated/ssh/authorized_keys is empty;" \
             "run './${progname} init' to create the lab server's key pair"
}

# The dev container mounts ~/.gitconfig. This is the only file the
# framework creates outside the checkout.
fed_require_gitconfig() {
  [ -e "${HOME}/.gitconfig" ] || : >"${HOME}/.gitconfig"
}

# Refuse to start a container that could change what the host runs (see
# framework/check-mounts.py).
fed_check_mounts() {
  fed_require_python
  "${fed_python}" framework/check-mounts.py || exit 1
}

# Start the dev container, which only docker-compose.yaml defines,
# whatever the topology.
fed_dev_up() {
  fed_check_mounts
  fed_require_gitconfig
  COMPOSE_IGNORE_ORPHANS=true \
      docker compose -f framework/docker-compose.yaml up --detach dev
}

# Serve the tests' requests to control the services
# (framework/control.py) in the background, for as long as they run,
# with what it says appended to file $1, if given, and otherwise on
# stderr. A background job ignores SIGINT, so the helper outlives an
# interrupt long enough to put back what the tests leave behind.
fed_control_start() {
  fed_require_python
  if [ -n "${1:-}" ]; then
    "${fed_python}" framework/control.py serve 2>>"$1" &
  else
    "${fed_python}" framework/control.py serve &
  fi
  fed_control_pid=$!
}

# Stop the control helper, if it runs.
fed_control_stop() {
  [ -n "${fed_control_pid:-}" ] || return 0
  kill "${fed_control_pid}" 2>/dev/null || true
  wait "${fed_control_pid}" 2>/dev/null || true
  fed_control_pid=""
}

# Interrupt the tests that the `test` command runs in the dev container,
# and wait for them to end, while the control helper still serves what
# they put back. Docker passes no signal on to what `exec -T` runs, so
# an interrupt of the command alone would leave them running. If they
# have not ended after a minute, kill them; the helper puts back what
# they leave.
fed_test_interrupt() {
  docker compose -f framework/docker-compose.yaml exec -T dev sh -c '
      tests="^python3\.13 /scratch/test\.py"
      pkill -INT -f "${tests}" || exit 0
      echo "waiting for the tests in the dev container to end" >&2
      n=0
      while pgrep -f "${tests}" >/dev/null; do
        if [ "${n}" -ge 60 ]; then
          echo "killing the tests in the dev container" >&2
          exec pkill -KILL -f "${tests}"
        fi
        sleep 1
        n=$((n + 1))
      done' || true
}

# Copy stdin to stdout with the signature of each JWT in it cut out, as
# test.py cuts it out of what the tests print (redact() in
# framework/testlib/report.py): a token that a server's log quotes would
# otherwise be good, for as long as it lasts, to whoever reads the log.
fed_redact_tokens() {
  sed -E 's/(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{8,}/\1.<signature>/g'
}

# Keep the run's record in framework/var/logs/$1/, once:
#
#   <svc>.log       each service's log since its container was created,
#                   with tokens' signatures cut out
#   ps.txt          the containers' states
#   containers.txt  how each container stands or ended, and whether the
#                   kernel killed it for want of memory
#   stats.txt       each container's memory use against its limit
#   shape.txt       the presets, topology, profiles, and images
#   config/         what fed.sh generated for the shape
#                   (framework/var/generated/), less keys, tokens, and
#                   the OIDC client's secret
#   results/        the rows of the suites that this run ran
#   commands*.log   every client command of each pass of the tests,
#                   with all it printed
#
# test.log and control.log, what the tests and the control helper
# print, are written there as the tests run (see the `test` command). The
# tests restart services but never recreate them, so a log covers the
# whole run, and the startup before it.
fed_save_logs() {
  [ -n "$1" ] && [ -z "${fed_logs_saved:-}" ] || return 0
  fed_logs_saved=yes
  dir="framework/var/logs/$1"
  mkdir -p "${dir}" || { warn "cannot save the services' logs in ${dir}/"; return 0; }
  {
    printf 'Presets:  %s\nTopology: %s\nProfiles: %s\nTag:      %s\n' \
        "${presets:-(default)}" "${fed_topology}" "${COMPOSE_PROFILES:-(none)}" \
        "${PELICAN_TAG}"
    printf 'Images:   %s %s %s %s\n' \
        "${IMAGE_ORIGIN}" "${IMAGE_CACHE}" "${IMAGE_DIRECTOR}" "${IMAGE_REGISTRY}"
  } >"${dir}/shape.txt"
  docker compose ps --all >"${dir}/ps.txt" 2>&1 || true
  ids=$(docker compose ps --all --quiet 2>/dev/null) || ids=""
  if [ -n "${ids}" ]; then
    # shellcheck disable=SC2086  # one container ID per word
    docker inspect --format \
        '{{index .Config.Labels "com.docker.compose.service"}}: {{.State.Status}}, exit code {{.State.ExitCode}}, OOM-killed {{.State.OOMKilled}}, started {{.State.StartedAt}}, finished {{.State.FinishedAt}}{{if .State.Health}}, {{.State.Health.Status}}{{end}}' \
        ${ids} >"${dir}/containers.txt" 2>&1 || true
    # shellcheck disable=SC2086
    docker stats --no-stream ${ids} >"${dir}/stats.txt" 2>&1 || true
  fi
  ( cd framework/var/generated \
      && find . -type f ! -path './ssh/*' ! -name '*.key' ! -name '*-key' \
          ! -name '*secret*' ! -name '*token*' ! -name '*.new' ) \
      | while read -r f; do
          mkdir -p "${dir}/config/$(dirname -- "${f}")" \
              && cp "framework/var/generated/${f}" "${dir}/config/${f}"
        done
  if [ -f "${dir}/.started" ]; then
    mkdir -p "${dir}/results"
    # The dev container writes these directories, so put-file.py copies
    # each file: it follows no symlink, not even one put in the file's
    # place after find looked.
    fed_require_python
    find framework/var/results -maxdepth 1 -type f -name '*.tsv' \
        -newer "${dir}/.started" \
        -exec "${fed_python}" framework/put-file.py {} "${dir}/results/" \;
    find framework/var/dev -maxdepth 1 -type f -name 'commands*.log' \
        -newer "${dir}/.started" \
        -exec "${fed_python}" framework/put-file.py {} "${dir}/" \;
    rm -f "${dir}/.started"
  fi
  for svc in $(docker compose ps --all --services 2>/dev/null); do
    { docker compose logs --no-color --no-log-prefix --timestamps "${svc}" 2>&1 \
          || warn "cannot save ${svc}'s log; see ${dir}/${svc}.log"; } \
        | fed_redact_tokens >"${dir}/${svc}.log"
  done
  printf 'Service logs are in %s/.\n' "${dir}"
}

# Warn (only) if the test data was built for a different shape.
fed_check_data_shape() {
  fed_require_python
  want=$("${fed_python}" framework/init-data.py --print-shape)
  have=""
  if [ -f framework/var/data/.init-shape ]; then
    have=$(cat framework/var/data/.init-shape)
  fi

  if [ -z "${have}" ]; then
    warn "no test data yet; run './${progname} init'"
  elif [ "${have}" != "${want}" ]; then
    warn "the test data was built for another shape"
    printf '  built for:   %s\n' "${have}" >&2
    printf '  starting:    %s\n' "${want}" >&2
    printf '  rebuild it:  ./%s init\n' "${progname}" >&2
  fi
}

# Under SERVER_DROP_PRIVILEGES, do what an admin would for the user
# Pelican drops to (`pelican`, uid 10941 in the images): let it write the
# stores and read the TLS key. Nothing more: whatever else Pelican needs
# is Pelican's to arrange (see framework/presets/server-unprivileged.sh).
# On a Linux host, Pelican may already have taken some of these over, so
# a failure only warns.
fed_prepare_unprivileged() {
  [ "${SERVER_DROP_PRIVILEGES}" = true ] || return 0
  for store in ${fed_stores}; do
    chmod 0777 "framework/var/data/${store}" 2>/dev/null \
        || warn "could not let the pelican user write framework/var/data/${store}"
  done
  if [ -f framework/var/certs/tls.key ]; then
    chmod 0444 framework/var/certs/tls.key 2>/dev/null \
        || warn "could not let the pelican user read framework/var/certs/tls.key"
  fi
}

# Check and prepare what the services need, for `up` and `restart`.
fed_preflight() {
  fed_require_python
  fed_require_certs
  fed_require_keys
  fed_require_lab_key
  fed_require_gitconfig
  fed_check_data_shape
  fed_prepare_unprivileged
}

#---------------------------------------------------------------------------
# init: each step runs only if its output is missing or out of date.
# Keys are never replaced (a cache's store is sealed to them). To start
# over, ./reset.sh discards everything.

# The `init` command.
fed_init() {
  [ $# -eq 0 ] || die_usage "init takes no arguments"

  fed_require_python

  # A new host needs only a new server certificate. Keeping the CA keeps
  # running services trusting whatever starts next.
  if ! fed_certs_fresh; then
    ./framework/init-certs.sh
  elif ! fed_certs_current; then
    ./framework/init-certs.sh --server
    printf 'Running services keep the certificate they started with,'
    printf ' which the CA still signs.\n'
  else
    printf 'Keeping the certificates in framework/var/certs/.\n'
  fi

  if fed_have_binaries; then
    printf 'Keeping the %s binaries in framework/var/bin/.\n' "${PELICAN_TAG#v}"
    # fed_have_binaries does not look for this one, so that a custom one
    # survives; fetch it only if there is none.
    if [ ! -x framework/var/bin/linux/pelican-server ]; then
      fed_fetch_binaries linux-server
    fi
  else
    fed_fetch_binaries
  fi

  fed_key_init

  if [ ! -s framework/var/generated/ssh/id_ed25519 ]; then
    printf 'Creating an SSH key pair for the lab server ...\n'
    rm -f framework/var/generated/ssh/id_ed25519 framework/var/generated/ssh/id_ed25519.pub
    ssh-keygen -q -t ed25519 -N '' -C pelican-test-framework \
        -f framework/var/generated/ssh/id_ed25519
  fi
  if [ ! -s framework/var/generated/ssh/authorized_keys ]; then
    cp framework/var/generated/ssh/id_ed25519.pub framework/var/generated/ssh/authorized_keys
  fi

  want=$("${fed_python}" framework/init-data.py --print-shape)
  if [ -f framework/var/data/.init-shape ] \
      && [ "$(cat framework/var/data/.init-shape)" = "${want}" ]; then
    printf 'Keeping the test data in framework/var/data/, built for this shape.\n'
  else
    "${fed_python}" framework/init-data.py
  fi

  fed_prepare_unprivileged
}

#---------------------------------------------------------------------------
# up and down.
#
# Both Compose files are one Compose project. Compose leaves behind
# services whose profile was turned off, so `down` enables every profile,
# and `up` removes unselected containers itself.

# Stop and remove every container, whatever its profile.
fed_down() {
  COMPOSE_PROFILES='*' docker compose down --remove-orphans "$@"
}

# Each of containers $@ as <ID>/<Compose service>, one per line.
fed_container_services() {
  docker inspect --format '{{.Id}}/{{index .Config.Labels "com.docker.compose.service"}}' "$@"
}

# Stop and remove this project's containers that the selection does not
# start, except dev (which `./fed.sh dev` runs under any topology).
fed_remove_unselected() {
  keep=" $(docker compose config --services | tr '\n' ' ') dev "
  ids=$(docker compose ps --all --quiet)
  [ -n "${ids}" ] || return 0

  gone=""
  gone_services=""
  # shellcheck disable=SC2086  # one container ID per word
  for ctr in $(fed_container_services ${ids}); do
    svc=${ctr#*/}
    case "${keep}" in
      *" ${svc} "*) continue ;;
    esac
    gone="${gone} ${ctr%%/*}"
    case "${gone_services} " in
      *" ${svc} "*) ;;
      *) gone_services="${gone_services} ${svc}" ;;
    esac
  done
  [ -n "${gone}" ] || return 0

  printf 'Removing what this selection does not start:%s\n' \
      "${gone_services}"
  # shellcheck disable=SC2086
  docker stop ${gone} >/dev/null
  # shellcheck disable=SC2086
  docker rm ${gone} >/dev/null
}

# Compose recreates a container when its definition changes, not when a
# mounted file does. After framework/var/generated/conf/ or instance/
# changes (see fed_conf_sum), name the Pelican services still running
# with the old configuration (same container ID as before `up`).
# framework/var/generated/conf-changed holds those IDs until none are
# left.
fed_up() {
  fed_check_mounts
  fed_remove_unselected

  before=""
  if [ -e framework/var/generated/conf-changed ]; then
    before=$(cat framework/var/generated/conf-changed)
    if [ -z "${before}" ]; then
      before=$(docker compose ps --quiet)
    fi
  fi

  # No --remove-orphans: it would kill a dev container started under
  # another topology.
  COMPOSE_IGNORE_ORPHANS=true docker compose up --detach "$@"

  stale=""
  stale_ids=""
  if [ -n "${before}" ]; then
    # shellcheck disable=SC2086  # one container ID per word
    for ctr in $(fed_container_services ${before} 2>/dev/null); do
      case " ${fed_services} " in
        *" ${ctr#*/} "*)
            stale="${stale} ${ctr#*/}"
            stale_ids="${stale_ids} ${ctr%%/*}" ;;
      esac
    done
  fi
  if [ -z "${stale}" ]; then
    rm -f framework/var/generated/conf-changed
    return 0
  fi
  # shellcheck disable=SC2086
  printf '%s\n' ${stale_ids} >framework/var/generated/conf-changed
  warn "framework/var/generated/ changed, but these services are still running" \
       "the old configuration:${stale}"
  printf '  Apply the change with: ./%s restart%s\n' "${progname}" "${stale}" >&2
}

#---------------------------------------------------------------------------
# Commands. Anything else goes to docker compose.

cmd="$1"; shift

case "${cmd}" in

  init)
    fed_init "$@"
    ;;

  keys)
    fed_keys "$@"
    ;;

  up)
    fed_preflight
    fed_up "$@"
    ;;

  down)
    fed_down "$@"
    ;;

  # Recreated, not restarted: fed_up tracks stale configuration by
  # container ID.
  restart)
    fed_preflight
    fed_up --force-recreate "$@"
    ;;

  dev|test)
    fed_dev_up
    logs=""
    if [ "${cmd}" = test ]; then
      # The services' logs are kept, pass or fail, but for a listing or
      # the usage (see fed_save_logs).
      case "${1:-}" in
        -h|--help|-l|--list) ;;
        *) logs=$(date -u +%Y%m%d-%H%M%S)${RESULTS_TAG:+"-${RESULTS_TAG}"} ;;
      esac
      # RESULTS_TAG goes through, to name the results (see
      # framework/testlib/report.py).
      set -- env ${RESULTS_TAG:+"RESULTS_TAG=${RESULTS_TAG}"} python3.13 /scratch/test.py "$@"
    fi
    [ $# -gt 0 ] || set -- bash -il
    # No TTY unless both ends are a terminal, so output can be captured.
    no_tty=-T
    if [ -t 0 ] && [ -t 1 ]; then
      no_tty=""
    fi
    if [ "${cmd}" = dev ]; then
      exec docker compose -f framework/docker-compose.yaml \
          exec ${no_tty:+"${no_tty}"} dev "$@"
    fi
    # The control helper's output is kept beside the tests', as
    # control.log. The logs are saved after the helper puts back what the
    # tests left.
    log_dir=""
    if [ -n "${logs}" ]; then
      log_dir="framework/var/logs/${logs}"
      mkdir -p "${log_dir}"
      # The results that this run writes are those newer than this.
      : >"${log_dir}/.started"
    fi
    fed_control_start "${log_dir:+${log_dir}/control.log}"
    trap 'fed_control_stop' EXIT
    trap 'fed_test_interrupt; fed_control_stop; fed_save_logs "${logs}"; exit 130' INT
    trap 'fed_test_interrupt; fed_control_stop; fed_save_logs "${logs}"; exit 143' TERM
    status=0
    if [ -n "${logs}" ]; then
      # What the tests print is kept beside the services' logs, as
      # test.log (see fed_save_logs), through a pipe, so the tests get
      # no TTY, and the subshell leaves the exit status in a file.
      {
        docker compose -f framework/docker-compose.yaml exec -T dev "$@" 2>&1 \
            || printf '%s\n' "$?" >"${log_dir}/.status"
      } | tee "${log_dir}/test.log"
      if [ -f "${log_dir}/.status" ]; then
        status=$(cat "${log_dir}/.status")
        rm -f "${log_dir}/.status"
      fi
    else
      docker compose -f framework/docker-compose.yaml \
          exec ${no_tty:+"${no_tty}"} dev "$@" || status=$?
    fi
    fed_control_stop
    fed_save_logs "${logs}"
    exit "${status}"
    ;;

  # In the foreground, for a test.py run by hand in the dev container.
  control)
    [ $# -eq 0 ] || die_usage "control takes no arguments"
    fed_require_python
    exec "${fed_python}" framework/control.py serve
    ;;

  status)
    docker compose ps
    printf '\nTopology: %s\nPresets:  %s\nProfiles: %s\nImages:\n' \
        "${fed_topology}" "${presets:-(default)}" \
        "${COMPOSE_PROFILES:-(none)}"
    if [ "${fed_topology}" = tiny ]; then
      printf '  %-9s %s\n' \
          fed "${IMAGE_ORIGIN}" \
          dev "${IMAGE_DEV}"
    elif [ "${fed_topology}" = standalone ]; then
      printf '  %-9s %s\n' \
          origin "${IMAGE_ORIGIN}" \
          dev    "${IMAGE_DEV}"
    else
      printf '  %-9s %s\n' \
          origin   "${IMAGE_ORIGIN}" \
          cache    "${IMAGE_CACHE}" \
          director "${IMAGE_DIRECTOR}" \
          registry "${IMAGE_REGISTRY}" \
          dev      "${IMAGE_DEV}"
    fi
    ;;

  *)
    docker compose "${cmd}" "$@"
    ;;

esac
