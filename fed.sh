#!/bin/sh
#
# Run a Pelican test federation: choose a shape with presets, then hand
# off to Docker Compose.
#
#   ./fed.sh init                 prepare certificates, binaries, keys, data
#   ./fed.sh init --force         ... redoing work already done
#   ./fed.sh up                   start the federation
#   ./fed.sh -p pstore -p ha up   start a different shape (remembered)
#   ./fed.sh -p default up        ... forgetting the remembered shape
#   ./fed.sh down                 stop everything
#   ./fed.sh restart              down, then up
#   ./fed.sh keys                 list issuer keys (also: init, add, rotate)
#   ./fed.sh dev                  open a shell in the dev container
#   ./fed.sh status               show containers, presets, and images
#   ./fed.sh logs -f origin-0     anything else goes to docker compose
#
# Run `init` with the same presets as `up`: the test data depends on them.

set -eu

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }
warn()      { printf '%s: warning: %s\n' "${progname}" "$*" >&2; }

# The usage text is the header comment, up to the first blank line.
usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
  printf '\nAvailable presets:'
  for preset in presets/*.sh; do
    preset=${preset##*/}
    preset=${preset%.sh}
    [ "${preset}" != default ] || continue
    printf ' %s' "${preset}"
  done
  printf '\n'
}

#---------------------------------------------------------------------------
# Every Pelican server the framework can start (`fed` is the `tiny`
# topology). Each owns issuer-keys/<svc> and state/<svc>.
#
# Group members must share a key:
#   directors  the discovery document publishes only director-0's JWKS
#   origins    both export the same prefixes, and the registry binds one
#              keyset per prefix

fed_services="director-0 director-1 registry origin-0 origin-1 cache-0 cache-1 fed"
fed_group_directors="director-0 director-1"
fed_group_origins="origin-0 origin-1"

fed_certs="certs/ca.crt certs/tls.crt certs/tls.key"

#---------------------------------------------------------------------------
# Compose profiles. Presets call fed_enable_profile.

fed_has_profile() {
  case ",${COMPOSE_PROFILES}," in
    *",$1,"*) return 0 ;;
  esac
  return 1
}

fed_enable_profile() {
  if ! fed_has_profile "$1"; then
    COMPOSE_PROFILES="${COMPOSE_PROFILES:+${COMPOSE_PROFILES},}$1"
  fi
}

#---------------------------------------------------------------------------
# Command line. Everything after the first non-option is for Compose.

# Ignore these if the caller's shell has them: the presets decide the
# profiles, TOPOLOGY decides the file, and the project is always `pelican`.
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

mkdir -p generated/conf
presets_given=""
if [ -n "${presets}" ]; then
  presets_given=yes
  for preset in ${presets}; do
    [ -f "./presets/${preset}.sh" ] || die_usage "no such preset: ${preset}"
  done
elif [ -f generated/presets ]; then
  presets=$(cat generated/presets)
fi

#---------------------------------------------------------------------------
# Settings, lowest precedence first: environment.cfg,
# environment.local.cfg, presets/default.sh, -p presets, then the
# caller's environment.
#
# Only the knobs below are taken from the caller's environment.
# MAX_CONCURRENT_ADS is not here: run.sh reads it inside the dev
# container.

fed_knobs="TOPOLOGY ORIGIN_VARIANT CACHE_VARIANT
           IMAGE_HUB PELICAN_TAG PELICAN_DEV_TAG
           PELICAN_LOG_LEVEL TZ_SERVICES TZ_STORAGE
           ORIGIN_MEM CACHE_MEM DIRECTOR_MEM REGISTRY_MEM DEV_MEM FED_MEM
           ORIGIN_ENABLE_ISSUER ORIGIN_ENABLE_OIDC PELICAN_SRC_DIR
           IMAGE_ORIGIN IMAGE_CACHE IMAGE_DIRECTOR IMAGE_REGISTRY IMAGE_DEV
           OBJECTS_PER_ORIGIN ADS_PER_SCENARIO
           URLS_PER_EXIST_AD URLS_PER_TOKEN_AD URLS_PER_DNE_AD"

# One variable per knob, so that a path with spaces survives.
for knob in ${fed_knobs}; do
  eval "fed_caller_${knob}=\${${knob}:-}"
done

set -a
# shellcheck disable=SC1091
. ./environment.cfg
if [ -f ./environment.local.cfg ]; then
  # shellcheck disable=SC1091
  . ./environment.local.cfg
fi
# shellcheck disable=SC1091
. ./presets/default.sh
for preset in ${presets}; do
  [ -f "./presets/${preset}.sh" ] || die "no such preset: ${preset}"
  # shellcheck disable=SC1090
  . "./presets/${preset}.sh"
done
for knob in ${fed_knobs}; do
  eval "[ -n \"\${fed_caller_${knob}}\" ]" || continue
  eval "${knob}=\${fed_caller_${knob}}"
done
set +a
export COMPOSE_PROFILES

# Naming the file also keeps Compose from picking up an override file.
case "${TOPOLOGY}" in
  full) COMPOSE_FILE=docker-compose.yaml ;;
  tiny) COMPOSE_FILE=docker-compose.tiny.yaml ;;
  *)    die "no such topology: ${TOPOLOGY} (try: full, tiny)" ;;
esac
fed_topology=${TOPOLOGY}
export COMPOSE_FILE

: "${IMAGE_DEV:=${IMAGE_HUB}/pelican-dev:${PELICAN_DEV_TAG}}"
: "${IMAGE_ORIGIN:=${IMAGE_HUB}/origin:${PELICAN_TAG}}"
: "${IMAGE_CACHE:=${IMAGE_HUB}/cache:${PELICAN_TAG}}"
: "${IMAGE_DIRECTOR:=${IMAGE_HUB}/director:${PELICAN_TAG}}"
: "${IMAGE_REGISTRY:=${IMAGE_HUB}/registry:${PELICAN_TAG}}"
export IMAGE_DEV IMAGE_ORIGIN IMAGE_CACHE IMAGE_DIRECTOR IMAGE_REGISTRY

#---------------------------------------------------------------------------
# Refuse combinations that would start "fine" and then misbehave.

# Docker would create a missing directory, leaving the role layer empty.
[ -d "config.d/origin/${ORIGIN_VARIANT}" ] \
    || die "no such origin variant: ${ORIGIN_VARIANT}"
[ -d "config.d/cache/${CACHE_VARIANT}" ] \
    || die "no such cache variant: ${CACHE_VARIANT}"

# Without the lab server, an `ssh` origin comes up healthy with no storage.
if [ "${ORIGIN_VARIANT}" = ssh ]; then
  if [ "${fed_topology}" = tiny ]; then
    die "the 'tiny' topology has no lab server for an 'ssh' origin"
  fi
  fed_has_profile lab \
      || die "an 'ssh' origin needs the lab server; use '-p ssh'"
fi

# fed_generate knows only two export tables: base's and pstore's.
if fed_has_profile multi && [ "${ORIGIN_VARIANT}" != pstore ]; then
  if grep -qs '^  Exports:' "config.d/origin/${ORIGIN_VARIANT}"/*.yaml; then
    die "config.d/origin/${ORIGIN_VARIANT}/ replaces Origin.Exports;" \
        "teach fed_generate about it before combining it with 'multi'"
  fi
fi

# Profiles add services that exist only in the full topology.
fed_warn_inert_profiles() {
  [ "${fed_topology}" = tiny ] || return 0
  inert=""
  for profile in ha multi monitoring lab; do
    if fed_has_profile "${profile}"; then
      inert="${inert} ${profile}"
    fi
  done
  [ -z "${inert}" ] || warn "the tiny topology ignores:${inert}"
}

# Remember the selection only once it has passed the checks above.
if [ -n "${presets_given}" ]; then
  printf '%s\n' "${presets}" >generated/presets
fi

# Compose refuses to start if the dev container's /app source is missing.
if [ ! -d "${PELICAN_SRC_DIR:-}" ]; then
  PELICAN_SRC_DIR="${PWD}/generated/pelican-src"
  mkdir -p "${PELICAN_SRC_DIR}"
fi
export PELICAN_SRC_DIR

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

# The issuer URL an origin advertises for federation prefix $1, derived
# as Pelican does. Tokens must match it exactly.
#
#   issuer enabled   <origin>/api/v1.0/issuer/ns<prefix>
#   no issuer        <origin>, or <origin>/api/v1.0/origin when the origin
#                    shares a process with a director (`tiny`)
fed_issuer_url() {
  case "${ORIGIN_ENABLE_ISSUER}" in
    true)
        printf '%s/api/v1.0/issuer/ns%s\n' "${fed_origin_url}" "$1" ;;
    *)
        if [ "${fed_topology}" = tiny ]; then
          printf '%s/api/v1.0/origin\n' "${fed_origin_url}"
        else
          printf '%s\n' "${fed_origin_url}"
        fi ;;
  esac
}

fed_generate() {
  directors="https://director-0:8444"
  if fed_has_profile ha; then
    directors="${directors} https://director-1:8444"
  fi

  {
    printf -- '---\n'
    printf '# Generated by %s. Do not edit.\n\n' "${progname}"
    printf 'Federation:\n  DirectorUrl: https://director-0:8444\n\n'
    printf 'Server:\n  DirectorUrls:\n'
    for d in ${directors}; do printf '    - %s\n' "${d}"; done
  } | fed_write generated/conf/50-topology.yaml

  fed_discovery_url="https://discovery:8444"
  fed_jwks_uri="https://director-0:8444/.well-known/issuer.jwks"

  {
    printf '{\n'
    printf '  "discovery_endpoint": "%s",\n' "${fed_discovery_url}"
    printf '  "director_endpoint": "https://director-0:8444",\n'
    printf '  "director_advertise_endpoints": [\n'
    first=1
    for d in ${directors}; do
      [ "${first}" -eq 1 ] || printf ',\n'
      first=0
      printf '    "%s"' "${d}"
    done
    printf '\n  ],\n'
    printf '  "namespace_registration_endpoint": "https://registry:8444",\n'
    printf '  "jwks_uri": "%s",\n' "${fed_jwks_uri}"
    printf '  "broker_endpoint": ""\n'
    printf '}\n'
  } | fed_write generated/discovery.json

  # The federation as a token issuer; see etc/nginx-discovery.conf.
  {
    printf '{\n'
    printf '  "issuer": "%s",\n' "${fed_discovery_url}"
    printf '  "jwks_uri": "%s"\n' "${fed_jwks_uri}"
    printf '}\n'
  } | fed_write generated/openid-configuration.json

  fed_origin_url="https://origin-0:8444"
  fed_federation_url="pelican://discovery:8444"
  fed_origin_key_dir=issuer-keys/origin-0
  if [ "${fed_topology}" = tiny ]; then
    fed_origin_url="https://fed:8444"
    fed_federation_url="pelican://fed:8444"
    fed_origin_key_dir=issuer-keys/fed
  fi

  # Read by run.sh and init-data.sh.
  fed_issuer_url /private | fed_write generated/origin-issuer-url
  printf '%s\n' "${fed_origin_key_dir}" | fed_write generated/origin-key-dir
  printf '%s\n' "${fed_federation_url}" | fed_write generated/federation-url

  # Two origins exporting the same prefixes must advertise one issuer.
  # Pinning IssuerUrls means restating the whole Exports list, since a
  # later layer replaces a list rather than merging into it.
  if [ "${fed_topology}" != tiny ] && fed_has_profile multi; then
    case "${ORIGIN_VARIANT}" in
      pstore) public_caps='"PublicReads", "Listings", "DirectReads", "Writes"'
              public_storage=/public
              private_storage=/private ;;
      *)      public_caps='"PublicReads", "Listings", "DirectReads"'
              public_storage=/data
              private_storage=/data ;;
    esac
    {
      printf -- '---\n'
      printf '# Generated by %s. Do not edit.\n\n' "${progname}"
      printf 'Origin:\n  Exports:\n'
      printf -- '    - FederationPrefix: /public\n'
      printf '      Capabilities: [%s]\n' "${public_caps}"
      case "${public_caps}" in
        *Writes*)
            printf '      IssuerUrls: ["%s"]\n' "$(fed_issuer_url /public)" ;;
      esac
      printf '      StoragePrefix: %s\n\n' "${public_storage}"
      printf -- '    - FederationPrefix: /private\n'
      printf '      Capabilities: ["Reads", "Writes", "Listings", "DirectReads"]\n'
      printf '      IssuerUrls: ["%s"]\n' "$(fed_issuer_url /private)"
      printf '      StoragePrefix: %s\n' "${private_storage}"
    } | fed_write generated/conf/60-origin-issuers.yaml
  else
    rm -f generated/conf/60-origin-issuers.yaml
  fi

  # The lab server's bind mount needs a source. `init` writes the real
  # one; with the lab server running, fed_require_lab_key insists on it.
  mkdir -p generated/ssh
  if ! fed_has_profile lab && [ ! -e generated/ssh/authorized_keys ]; then
    : >generated/ssh/authorized_keys
  fi
}

# Create every bind-mount source, so that Docker does not create them as
# root.
fed_prepare_dirs() {
  for svc in ${fed_services}; do
    mkdir -p "state/${svc}" "issuer-keys/${svc}"
  done
  mkdir -p \
      data/origin/0 data/origin/1 \
      data/cache/0 data/cache/1 data/cache/fed \
      data/lab-server \
      grafana
  # Written over SSH by `alice`, whose uid is unrelated to yours.
  chmod 0777 data/lab-server
}

# Services read generated/conf/ only at startup. Mark a change so that
# `up` can name the services still running the old configuration.
fed_conf_sum() { cat generated/conf/*.yaml 2>/dev/null | cksum; }

fed_conf_was=$(fed_conf_sum)
fed_generate
if [ "$(fed_conf_sum)" != "${fed_conf_was}" ]; then
  : >generated/conf-changed
fi
fed_prepare_dirs

#---------------------------------------------------------------------------
# Released binaries, for use outside the service containers:
#
#   bin/linux/    the client run.sh uses in the dev container
#   bin/<host>/   pelican-server for `keys`, plus a client for the host
#
# On a Linux host these are the same directory.

fed_os=$(uname -s)
fed_arch=$(uname -m)
case "${fed_arch}" in
  aarch64) fed_arch=arm64 ;;
esac
fed_host_bin="bin/$(printf '%s' "${fed_os}" | tr '[:upper:]' '[:lower:]')"

# Unpack binary $1 at version $2 for OS $3 into bin/<os>.
fed_fetch_archive() {
  tarball="$1_$3_${fed_arch}.tar.gz"
  dir="bin/$(printf '%s' "$3" | tr '[:upper:]' '[:lower:]')"
  mkdir -p "${dir}"
  ( cd "${dir}" || exit 1
    curl -fSL -O \
        "https://github.com/PelicanPlatform/pelican/releases/download/v$2/${tarball}"
    tar xzf "${tarball}"
    mv "$1-$2/$1" .
    rm -rf "${tarball}" "$1-$2" )
}

# True if bin/ holds PELICAN_TAG's binaries. Only the host's
# pelican-server is version-checked, so a custom bin/linux/pelican
# survives `init`.
fed_have_binaries() {
  [ -x bin/linux/pelican ] || return 1
  [ -e bin/linux/stash_plugin ] || return 1
  [ -x "${fed_host_bin}/pelican" ] || return 1
  [ -x "${fed_host_bin}/pelican-server" ] || return 1
  "${fed_host_bin}/pelican-server" --version 2>/dev/null \
      | grep -qxF "Version: ${PELICAN_TAG#v}" || return 1
  return 0
}

fed_fetch_binaries() {
  version=${PELICAN_TAG#v}
  printf 'Downloading the %s binaries ...\n' "${version}"

  # The client dispatches on its name; run.sh invokes stash_plugin.
  fed_fetch_archive pelican "${version}" Linux
  ln -sf pelican bin/linux/stash_plugin

  fed_fetch_archive pelican-server "${version}" "${fed_os}"
  if [ "${fed_os}" != Linux ]; then
    fed_fetch_archive pelican "${version}" "${fed_os}"
    ln -sf pelican "${fed_host_bin}/stash_plugin"
  fi
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
# The .jwks beside each key is for `keys list`; Pelican ignores it.

fed_key_check_target() {
  case "$1" in
    directors|origins|all) return 0 ;;
  esac
  for svc in ${fed_services}; do
    if [ "${svc}" = "$1" ]; then
      return 0
    fi
  done
  die_usage "no such key target: $1" \
      "(try: ${fed_services} directors origins all)"
}

# The directories that must hold the same key as keyset $1. Used in
# command substitutions, so it must not die.
# shellcheck disable=SC2086  # the lists are meant to be split
fed_key_dirs() {
  case "$1" in
    directors) printf '%s ' ${fed_group_directors} ;;
    origins)   printf '%s ' ${fed_group_origins} ;;
    *)         printf '%s ' "$1" ;;
  esac
}

# The keysets that target $1 names. `all` is one key per group plus one
# per ungrouped service, not one key for everything.
fed_key_sets() {
  case "$1" in
    all) printf 'directors origins'
         for svc in ${fed_services}; do
           case " ${fed_group_directors} ${fed_group_origins} " in
             *" ${svc} "*) continue ;;
           esac
           printf ' %s' "${svc}"
         done
         printf '\n' ;;
    *)   printf '%s\n' "$1" ;;
  esac
}

fed_key_prefixes() {
  for dir in "$@"; do
    for key in "issuer-keys/${dir}"/[0-9][0-9]-*.pem; do
      [ -e "${key}" ] || continue
      key=$(basename -- "${key}")
      printf '%s\n' "${key%%-*}"
    done
  done
}

# Create key $1 in the first directory named and copy it to the rest.
# `key create` exists only in pelican-server.
fed_key_make() {
  name=$1; shift
  first=$1

  [ -x "${fed_host_bin}/pelican-server" ] \
      || die "${fed_host_bin}/pelican-server is missing;" \
             "run './${progname} init' first"

  # `key create` will not overwrite a .jwks left behind by a deleted .pem.
  if [ -e "issuer-keys/${first}/${name}.jwks" ] \
      && [ ! -e "issuer-keys/${first}/${name}.pem" ]; then
    rm -f "issuer-keys/${first}/${name}.jwks"
  fi

  printf 'Creating issuer-keys/%s/%s.pem ...\n' "${first}" "${name}"
  "${fed_host_bin}/pelican-server" key create \
      --private-key "issuer-keys/${first}/${name}.pem" \
      --public-key "issuer-keys/${first}/${name}.jwks" >/dev/null

  [ -f "issuer-keys/${first}/${name}.pem" ] \
      || die "issuer-keys/${first}/${name}.pem was not created"

  chmod 0640 "issuer-keys/${first}/${name}.pem"
  chmod 0644 "issuer-keys/${first}/${name}.jwks"

  shift
  for dir in "$@"; do
    printf 'Copying it to issuer-keys/%s/ ...\n' "${dir}"
    cp "issuer-keys/${first}/${name}.pem" "issuer-keys/${dir}/${name}.pem"
    cp "issuer-keys/${first}/${name}.jwks" "issuer-keys/${dir}/${name}.jwks"
  done
}

# True if directory $1 holds an NN-*.pem. Keys Pelican generated for
# itself (pelican_generated_*.pem) do not count: Pelican creates one in
# an empty directory, which would silently split a group.
fed_key_dir_has_key() {
  for key in "issuer-keys/$1"/[0-9][0-9]-*.pem; do
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
    printf 'Copying issuer-keys/%s/ to issuer-keys/%s/ ...\n' \
        "${source_dir}" "${dir}"
    for key in "issuer-keys/${source_dir}"/[0-9][0-9]-*.pem \
               "issuer-keys/${source_dir}"/[0-9][0-9]-*.jwks; do
      [ -e "${key}" ] || continue
      cp "${key}" "issuer-keys/${dir}/"
    done
  done
}

# Every service gets a key, whether or not the current shape starts it.
fed_key_init() {
  for group in directors origins; do
    fed_key_init_group "${group}"
  done

  for svc in ${fed_services}; do
    case " ${fed_group_directors} ${fed_group_origins} " in
      *" ${svc} "*) continue ;;
    esac
    if ! fed_key_dir_has_key "${svc}"; then
      fed_key_make 50-initial "${svc}"
    fi
  done
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

fed_key_new() {
  verb=$1
  target=${2:-}
  [ -n "${target}" ] \
      || die_usage "keys ${verb} needs a target" \
                   "(try: directors, origins, all, or a service name)"
  fed_key_check_target "${target}"

  stamp=$(date +%Y%m%d-%H%M%S)
  for keyset in $(fed_key_sets "${target}"); do
    fed_key_new_set "${verb}" "${keyset}" "${stamp}"
  done

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

fed_key_list() {
  for svc in ${fed_services}; do
    printf '%s\n' "${svc}"
    active=" (active)"
    found=""
    for key in "issuer-keys/${svc}"/*.pem; do
      [ -e "${key}" ] || continue
      found=yes
      kid="no .jwks"
      if [ -f "${key%.pem}.jwks" ]; then
        kid=$(sed -n 's/.*"kid"[^"]*"\([^"]*\)".*/\1/p' "${key%.pem}.jwks" \
            | head -1)
      fi
      printf '  %-28s %s%s\n' "$(basename -- "${key}")" "${kid}" "${active}"
      active=""
    done
    [ -n "${found}" ] || printf '  (no keys)\n'
  done
  printf '\nThe first key listed for a service is the one it signs with.\n'
}

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

fed_running_services() {
  if [ "${fed_topology}" = tiny ]; then
    printf '%s\n' fed
    return 0
  fi
  printf '%s\n' director-0 registry origin-0 cache-0
  if fed_has_profile ha; then
    printf '%s\n' director-1
  fi
  if fed_has_profile multi; then
    printf '%s\n' origin-1 cache-1
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

fed_require_certs() {
  for cert in ${fed_certs}; do
    [ -f "${cert}" ] || die "${cert} is missing; run './${progname} init'"
  done
  if ! fed_certs_fresh; then
    warn "certs/ has expired or expires within a day;" \
         "replace it with './${progname} init'"
  fi
}

fed_require_keys() {
  missing=""
  for svc in $(fed_running_services); do
    if ! fed_key_dir_has_key "${svc}"; then
      missing="${missing} ${svc}"
    fi
  done
  [ -z "${missing}" ] \
      || die "no issuer key for:${missing}; run './${progname} keys init'"
}

fed_require_lab_key() {
  fed_has_profile lab || return 0
  [ -s generated/ssh/authorized_keys ] \
      || die "generated/ssh/authorized_keys is empty;" \
             "run './${progname} init' to create the lab server's key pair"
}

# The dev container mounts ~/.gitconfig. This is the only file the
# framework creates outside the checkout.
fed_require_gitconfig() {
  [ -e "${HOME}/.gitconfig" ] || : >"${HOME}/.gitconfig"
}

# Warn (only) if the test data was built for a different shape.
fed_check_data_shape() {
  want=$(./init-data.sh --print-shape)
  have=""
  if [ -f data/.init-shape ]; then
    have=$(cat data/.init-shape)
  fi

  if [ -z "${have}" ]; then
    warn "no test data yet; run './${progname} init'"
  elif [ "${have}" != "${want}" ]; then
    warn "the test data was built for another shape"
    printf '  built for:   %s\n' "${have}" >&2
    printf '  starting:    %s\n' "${want}" >&2
    printf '  rebuild it:  ./%s init --force\n' "${progname}" >&2
  fi
}

fed_preflight() {
  fed_warn_inert_profiles
  fed_require_certs
  fed_require_keys
  fed_require_lab_key
  fed_require_gitconfig
  fed_check_data_shape
}

#---------------------------------------------------------------------------
# init: each step is skipped if its output already exists. --force
# redoes the certificates, binaries, and test data, but never replaces
# keys (a cache's store is sealed to them); ./reset.sh does that.

fed_init() {
  force=""
  kept=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -f|--force) force=yes; shift ;;
      *) die_usage "init takes only --force" ;;
    esac
  done

  fed_warn_inert_profiles

  if [ -z "${force}" ] && fed_certs_fresh; then
    printf 'Keeping the certificates in certs/.\n'
    kept=yes
  else
    ( cd certs && ./init.sh )
  fi

  if [ -z "${force}" ] && fed_have_binaries; then
    printf 'Keeping the %s binaries in bin/.\n' "${PELICAN_TAG#v}"
    kept=yes
  else
    fed_fetch_binaries
  fi

  fed_key_init

  if [ ! -s generated/ssh/id_ed25519 ]; then
    printf 'Creating an SSH key pair for the lab server ...\n'
    rm -f generated/ssh/id_ed25519 generated/ssh/id_ed25519.pub
    ssh-keygen -q -t ed25519 -N '' -C pelican-test-framework \
        -f generated/ssh/id_ed25519
  fi
  if [ ! -s generated/ssh/authorized_keys ]; then
    cp generated/ssh/id_ed25519.pub generated/ssh/authorized_keys
  fi

  if [ -z "${force}" ] && [ -f data/.init-shape ] \
      && [ "$(cat data/.init-shape)" = "$(./init-data.sh --print-shape)" ]; then
    printf 'Keeping the test data in data/, built for this shape.\n'
    kept=yes
  else
    ./init-data.sh
  fi

  if [ -n "${kept}" ]; then
    printf '\nTo rebuild what was kept: ./%s init --force\n' "${progname}"
  fi
}

#---------------------------------------------------------------------------
# up and down.
#
# Both topologies are one Compose project. Compose leaves behind services
# whose profile was turned off, so `down` enables every profile, and `up`
# removes unselected containers itself.

fed_down() {
  COMPOSE_PROFILES='*' docker compose down --remove-orphans "$@"
}

# Stop and remove this project's containers that the selection does not
# start, except dev (which `./fed.sh dev` runs under either topology).
fed_remove_unselected() {
  keep=" $(docker compose config --services | tr '\n' ' ') dev "
  ids=$(docker compose ps --all --quiet)
  [ -n "${ids}" ] || return 0

  gone=""
  gone_services=""
  # shellcheck disable=SC2086  # one container ID per word
  for ctr in $(docker inspect --format \
      '{{.Id}}/{{index .Config.Labels "com.docker.compose.service"}}' \
      ${ids}); do
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
# mounted file does. After generated/conf/ changes, name the Pelican
# services still running with the old configuration (same container ID
# as before `up`). generated/conf-changed holds those IDs until none are
# left.
fed_up() {
  fed_remove_unselected

  before=""
  if [ -e generated/conf-changed ]; then
    before=$(cat generated/conf-changed)
    if [ -z "${before}" ]; then
      before=$(docker compose ps --quiet)
    fi
  fi

  # No --remove-orphans: it would kill a dev container started under the
  # other topology.
  COMPOSE_IGNORE_ORPHANS=true docker compose up --detach "$@"

  stale=""
  stale_ids=""
  if [ -n "${before}" ]; then
    # shellcheck disable=SC2086  # one container ID per word
    for ctr in $(docker inspect --format \
        '{{.Id}}/{{index .Config.Labels "com.docker.compose.service"}}' \
        ${before} 2>/dev/null); do
      case " ${fed_services} " in
        *" ${ctr#*/} "*)
            stale="${stale} ${ctr#*/}"
            stale_ids="${stale_ids} ${ctr%%/*}" ;;
      esac
    done
  fi
  if [ -z "${stale}" ]; then
    rm -f generated/conf-changed
    return 0
  fi
  # shellcheck disable=SC2086
  printf '%s\n' ${stale_ids} >generated/conf-changed
  warn "generated/conf/ changed, but these services are still running" \
       "the old configuration:${stale}"
  printf '  Apply the change with: ./%s restart\n' "${progname}" >&2
}

#---------------------------------------------------------------------------
# Sub-commands. Anything else goes to docker compose.

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

  restart)
    fed_preflight
    fed_down "$@"
    fed_up "$@"
    ;;

  dev)
    # dev is defined only in docker-compose.yaml, whatever the topology.
    fed_require_gitconfig
    COMPOSE_IGNORE_ORPHANS=true \
        docker compose -f docker-compose.yaml up --detach dev
    exec docker compose -f docker-compose.yaml exec -it dev bash -il
    ;;

  status)
    fed_warn_inert_profiles
    docker compose ps
    printf '\nTopology: %s\nPresets:  %s\nProfiles: %s\nImages:\n' \
        "${fed_topology}" "${presets:-(default)}" \
        "${COMPOSE_PROFILES:-(none)}"
    if [ "${fed_topology}" = tiny ]; then
      printf '  %-9s %s\n' \
          fed "${IMAGE_ORIGIN}" \
          dev "${IMAGE_DEV}"
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
