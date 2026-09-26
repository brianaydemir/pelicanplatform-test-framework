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
#   ./fed.sh test [ARG]...        run the tests (./test.py) in the dev container
#   ./fed.sh status               show containers, presets, and images
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
# The `origins` group's members must share a key: both export the same
# prefixes, and the registry binds one keyset per prefix. origin-2
# (`topo-multi-owner`) is another owner, with prefixes and a key of its
# own.

fed_services="director-0 registry origin-0 origin-1 origin-2 cache-0 cache-1 fed"
fed_group_origins="origin-0 origin-1"

# Key holders that are not Pelican servers: `issuer` is the external token
# issuer that the discovery host serves (`auth-external-issuer`).
fed_key_others="issuer"

fed_certs="framework/var/certs/ca.crt framework/var/certs/tls.crt framework/var/certs/tls.key"

#---------------------------------------------------------------------------
# Compose profiles. Presets call fed_enable_profile. Every profile either
# Compose file defines:
#
#   multi-origin  origin-1, a replica of origin-0
#   multi-cache   cache-1
#   multi-owner   origin-2, another owner
#   lab           lab-server, the SSH host
#   metadata      metadata and metadata-verifier, the metadata receiver
#   monitoring    grafana
#   webdav        webdav, the httpsv2 origins' backend
#   s3            s3, the s3v2 origins' backend

fed_profiles="multi-origin multi-cache multi-owner lab metadata monitoring webdav s3"

fed_has_profile() {
  case ",${COMPOSE_PROFILES}," in
    *",$1,"*) return 0 ;;
  esac
  return 1
}

# A profile that no Compose file defines would start nothing, silently.
fed_enable_profile() {
  case " ${fed_profiles} " in
    *" $1 "*) ;;
    *) die "no such Compose profile: $1 (try: ${fed_profiles})" ;;
  esac
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

# The presets that replace preset $1, if it is one of the names that the
# presets had before they took a prefix naming what they change. None
# replace `ha`, which was removed, or `public-ro`, whose read-only /public
# is now the default.
fed_renamed_preset() {
  case "$1" in
    tiny)        echo topo-tiny ;;
    ha)          echo ;;
    multi)       echo topo-multi-origin topo-multi-cache ;;
    xrootd)      echo origin-xrootd cache-xrootd ;;
    pstore)      echo origin-pstore ;;
    ssh)         echo origin-ssh ;;
    posc)        echo origin-posc ;;
    metadata)    echo origin-metadata ;;
    metadata-tx) echo origin-metadata-tx ;;
    public-ro)   echo ;;
    revalidate)  echo origin-max-age ;;
    oidc)        echo auth-oidc ;;
    monitoring)  echo with-grafana ;;
    lab)         echo with-lab ;;
    *)           return 1 ;;
  esac
}

fed_preset_exists() {
  [ -f "local/presets/$1.sh" ] || [ -f "framework/presets/$1.sh" ]
}

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
  elif fed_new=$(fed_renamed_preset "$1"); then
    [ -n "${fed_new}" ] || die_usage "no such preset: $1; it was removed"
    # shellcheck disable=SC2086  # one preset per word
    die_usage "no such preset: $1; it was renamed, so use:" \
              "$(printf -- '-p %s\n' ${fed_new} | paste -sd ' ' -)"
  else
    die_usage "no such preset: $1"
  fi
}

# Set fed_migrated to presets $@, with each old name replaced by its new
# ones (if any), and fed_changed if any was. Under `tiny`, whose cache is
# always V2, `xrootd` meant only the origin. None left is `default`.
fed_migrate_presets() {
  fed_migrated=""
  fed_changed=""
  for fed_old in "$@"; do
    if ! fed_preset_exists "${fed_old}" && fed_new=$(fed_renamed_preset "${fed_old}"); then
      if [ "${fed_old}" = xrootd ]; then
        case " $* " in
          *" tiny "*) fed_new=origin-xrootd ;;
        esac
      fi
      if [ -n "${fed_new}" ]; then
        fed_migrated="${fed_migrated}${fed_migrated:+ }${fed_new}"
      fi
      fed_changed=yes
    else
      fed_migrated="${fed_migrated}${fed_migrated:+ }${fed_old}"
    fi
  done
  : "${fed_migrated:=default}"
}

mkdir -p framework/var/generated/conf
presets_given=""
if [ -n "${presets}" ]; then
  presets_given=yes
elif [ -f framework/var/generated/presets ]; then
  presets=$(cat framework/var/generated/presets)
  # shellcheck disable=SC2086  # one preset per word
  fed_migrate_presets ${presets}
  if [ -n "${fed_changed}" ]; then
    warn "the remembered presets have been renamed or removed: '${presets}'" \
         "is now '${fed_migrated}'"
    presets=${fed_migrated}
    # Remembered again below, once the new names pass the checks.
    presets_given=yes
  fi
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
           ORIGIN_MEM CACHE_MEM DIRECTOR_MEM REGISTRY_MEM DEV_MEM FED_MEM
           IMAGE_ORIGIN IMAGE_CACHE IMAGE_DIRECTOR IMAGE_REGISTRY IMAGE_DEV"

# Knobs that choose one of several things, which no two presets may both
# choose: `-p origin-pstore -p origin-ssh` would otherwise quietly be
# `origin-ssh`. So that choosing the default (`-p origin-posixv2`) counts
# too, each of these knobs is `fed-unchosen` while a preset is sourced,
# which is what a preset that reads one sees.
fed_exclusive_knobs="TOPOLOGY ORIGIN_VARIANT CACHE_VARIANT"

# One variable per knob, so that a path with spaces survives.
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

# Naming the file also keeps Compose from picking up an override file.
case "${TOPOLOGY}" in
  full) COMPOSE_FILE=framework/docker-compose.yaml ;;
  tiny) COMPOSE_FILE=framework/docker-compose.tiny.yaml ;;
  *)    die "no such topology: ${TOPOLOGY} (try: full, tiny)" ;;
esac
fed_topology=${TOPOLOGY}
# The project directory is framework/, where the file is.
export COMPOSE_FILE

: "${IMAGE_DEV:=${IMAGE_HUB}/pelican-dev:${PELICAN_DEV_TAG}}"
: "${IMAGE_ORIGIN:=${IMAGE_HUB}/origin:${PELICAN_TAG}}"
: "${IMAGE_CACHE:=${IMAGE_HUB}/cache:${PELICAN_TAG}}"
: "${IMAGE_DIRECTOR:=${IMAGE_HUB}/director:${PELICAN_TAG}}"
: "${IMAGE_REGISTRY:=${IMAGE_HUB}/registry:${PELICAN_TAG}}"
export IMAGE_DEV IMAGE_ORIGIN IMAGE_CACHE IMAGE_DIRECTOR IMAGE_REGISTRY

#---------------------------------------------------------------------------
# Refuse combinations that would start "fine" and then misbehave. Presets
# of your own can set the knobs too, so the checks are on the knobs, and
# the messages name the presets that set them.

for fed_knob in ORIGIN_ENABLE_ISSUER ENABLE_OIDC EXTERNAL_ISSUER \
    ORIGIN_DISABLE_DIRECT_CLIENTS ORIGIN_POSC ORIGIN_METADATA ORIGIN_MULTIUSER \
    SERVER_DROP_PRIVILEGES; do
  eval "fed_value=\${${fed_knob}}"
  # shellcheck disable=SC2154  # assigned by the eval above
  case "${fed_value}" in
    true|false) ;;
    *) die "${fed_knob} must be 'true' or 'false', not '${fed_value}'" ;;
  esac
done

# Docker would create a missing directory, leaving the role layer empty.
[ -d "framework/config.d/origin/${ORIGIN_VARIANT}" ] \
    || die "no such origin variant: ${ORIGIN_VARIANT}"
[ -d "framework/config.d/cache/${CACHE_VARIANT}" ] \
    || die "no such cache variant: ${CACHE_VARIANT}"

# fed_generate writes Origin.Exports for every variant. A role layer's
# would lose to it anyway.
if grep -qs '^  Exports:' "framework/config.d/origin/${ORIGIN_VARIANT}"/*.yaml; then
  die "framework/config.d/origin/${ORIGIN_VARIANT}/ sets Origin.Exports, which" \
      "fed.sh generates; teach fed_generate about the variant instead"
fi

# The namespaces the origins export, each once: fed_namespaces.
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
fed_count_namespaces() {
  # shellcheck disable=SC2086  # one namespace per word
  set -- ${fed_namespaces}
  printf '%s\n' "$#"
}

fed_exports_namespace() {
  case " ${fed_namespaces} " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

# Set fed_caps to the capabilities the origins give namespace $1 (one of
# those above). /public takes no writes, and so has no issuer. Without
# direct clients, Pelican allows only reads.
fed_namespace_caps() {
  case "$1" in
    public)
      fed_caps=PublicReads
      if [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = false ]; then
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
    multi-origin) echo "'topo-multi-origin'" ;;
    multi-cache)  echo "'topo-multi-cache'" ;;
    multi-owner)  echo "'topo-multi-owner'" ;;
    lab)          echo "'with-lab' or 'origin-ssh'" ;;
    metadata)     echo "'origin-metadata' or 'origin-metadata-tx'" ;;
    monitoring)   echo "'with-grafana'" ;;
    webdav)       echo "'origin-httpsv2'" ;;
    s3)           echo "'origin-s3v2'" ;;
  esac
}

# The tiny topology is one container, with no room for another service,
# and its cache is always V2. Of the profiles, only the metadata receiver
# works with it.
if [ "${fed_topology}" = tiny ]; then
  for fed_profile in ${fed_profiles}; do
    [ "${fed_profile}" != metadata ] || continue
    if fed_has_profile "${fed_profile}"; then
      die "the 'tiny' topology is one container, so it cannot add profile" \
          "'${fed_profile}' ($(fed_profile_presets "${fed_profile}"))"
    fi
  done
  [ "${CACHE_VARIANT}" = v2 ] \
      || die "the 'tiny' topology's cache is always V2, so CACHE_VARIANT" \
             "('cache-xrootd') must be 'v2', not '${CACHE_VARIANT}'"
  [ "${EXTERNAL_ISSUER}" = false ] \
      || die "the 'tiny' topology has no discovery host to serve the external" \
             "issuer ('auth-external-issuer')"
fi

# Without its backend, an origin comes up healthy with no storage.
case "${ORIGIN_VARIANT}" in
  ssh)     fed_has_profile lab \
               || die "an 'ssh' origin needs the lab server; use '-p origin-ssh'" ;;
  httpsv2) fed_has_profile webdav \
               || die "an 'httpsv2' origin needs its WebDAV server; use '-p origin-httpsv2'" ;;
  s3v2)    fed_has_profile s3 \
               || die "an 's3v2' origin needs its S3 server; use '-p origin-s3v2'" ;;
esac

# Pelican's httpsv2 backend takes only one export.
if [ "${ORIGIN_VARIANT}" = httpsv2 ] && [ "$(fed_count_namespaces)" -ne 1 ]; then
  die "an 'httpsv2' origin takes one export, but ORIGIN_NAMESPACES is" \
      "'${fed_namespaces}'"
fi

# Only the posixv2 origin has POSC and publishes metadata.
if [ "${ORIGIN_POSC}" = true ] || [ "${ORIGIN_METADATA}" = true ]; then
  [ "${ORIGIN_VARIANT}" = posixv2 ] \
      || die "POSC and metadata publishing need the 'posixv2' origin," \
             "not '${ORIGIN_VARIANT}'"
fi
if [ "${ORIGIN_METADATA}" = true ]; then
  fed_has_profile metadata \
      || die "ORIGIN_METADATA needs the metadata receiver; use '-p origin-metadata'"
fi

# The XRootD origin has no Origin.CacheControl.
if [ -n "${ORIGIN_CACHE_CONTROL}" ] && [ "${ORIGIN_VARIANT}" = posix ]; then
  die "the 'posix' (XRootD) origin sends no Cache-Control; ORIGIN_CACHE_CONTROL" \
      "('origin-max-age') needs a native origin"
fi

# Pelican refuses DisableDirectClients with exports that mix public and
# token-protected reads, and only native origins enforce it.
if [ "${ORIGIN_DISABLE_DIRECT_CLIENTS}" = true ]; then
  [ "$(fed_count_namespaces)" -eq 1 ] \
      || die "ORIGIN_DISABLE_DIRECT_CLIENTS ('origin-no-direct') takes one" \
             "namespace, but ORIGIN_NAMESPACES is '${fed_namespaces}'"
  [ "${ORIGIN_VARIANT}" != posix ] \
      || die "the 'posix' (XRootD) origin ignores Origin.DisableDirectClients" \
             "('origin-no-direct'); it needs a native origin"
fi

# Nothing but the running origin can write a pstore, so the transfers
# suite seeds each protected namespace through its writes. /public shares
# /protected-a's storage (see fed_storage_prefix).
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
        "ORIGIN_NAMESPACES ('${fed_namespaces}') has no /protected-a"
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
fi

# posixv2 multiuser switches to each user's IDs, which needs root.
if [ "${ORIGIN_MULTIUSER}" = true ]; then
  [ "${ORIGIN_VARIANT}" = posixv2 ] \
      || die "ORIGIN_MULTIUSER ('origin-multiuser') needs the 'posixv2' origin," \
             "not '${ORIGIN_VARIANT}'"
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

# Remember the selection only once it has passed the checks above.
if [ -n "${presets_given}" ]; then
  printf '%s\n' "${presets}" >framework/var/generated/presets
fi

# Compose refuses to start if the dev container's /app source is missing.
if [ ! -d "${PELICAN_SRC_DIR:-}" ]; then
  PELICAN_SRC_DIR="${PWD}/framework/var/generated/pelican-src"
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

# The arguments as a YAML flow sequence of strings, e.g. ["Reads", "Writes"].
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
        printf '%s/api/v1.0/issuer/ns%s\n' "${fed_origin_url}" "$1" ;;
    *)
        if [ "${fed_topology}" = tiny ]; then
          printf '%s/api/v1.0/origin\n' "${fed_origin_url}"
        else
          printf '%s\n' "${fed_origin_url}"
        fi ;;
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
  if [ "${ORIGIN_VARIANT}" = posix ]; then
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

# A line of framework/var/generated/caches for service $1: its data URL
# and its kind, v2 (native) or xrootd. An XRootD cache serves on a port
# of its own. The `tiny` topology's cache is always native and shares a
# process with the director, so it serves only under
# /api/v1.0/cache/data/<discovery host>.
fed_cache_line() {
  if [ "${fed_topology}" = tiny ]; then
    printf '%s https://%s:8444/api/v1.0/cache/data/%s v2\n' \
        "$1" "$1" "${fed_discovery_url#https://}"
  elif [ "${CACHE_VARIANT}" = xrootd ]; then
    printf '%s https://%s:8442 xrootd\n' "$1" "$1"
  else
    printf '%s https://%s:8444 v2\n' "$1" "$1"
  fi
}

# Write stdin to file $2 in service $1's generated layer,
# framework/var/generated/instance/<svc>/ (its /fed/generated.instance),
# or remove the file: fed_instance_rm. fed_prepare_dirs makes the
# directories, which are never replaced, since each is bind-mounted.
fed_instance_write() {
  fed_write "framework/var/generated/instance/$1/$2"
}

fed_instance_rm() {
  rm -f "framework/var/generated/instance/$1/$2"
}

# Where namespace $1 is in an origin's store: the StoragePrefix of its
# export. A pstore gives each protected namespace storage of its own, and
# /public reads /protected-a's, since nothing can write /public. The
# httpsv2 and s3v2 origins' backends serve an origin's store at the root
# of its URL or bucket; every other origin mounts its store at /data.
fed_storage_prefix() {
  case "${ORIGIN_VARIANT}:$1" in
    pstore:public) printf '/protected-a\n' ;;
    pstore:*)      printf '/%s\n' "$1" ;;
    httpsv2:*|s3v2:*) printf '/\n' ;;
    *)             printf '/data\n' ;;
  esac
}

# One entry of Origin.Exports: federation prefix $1, with namespace $2's
# capabilities (see fed_namespace_caps) and storage prefix $3. If Pelican
# gives it an issuer, which it does only for an export that takes writes
# or needs a token to read, $4 is the issuer URL to pin and $5 the JWKS
# file whose keys that issuer adds; either may be empty.
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
}

fed_exports_header() {
  printf -- '---\n'
  printf '# Generated by %s. Do not edit.\n\n' "${progname}"
  printf 'Origin:\n  Exports:\n'
}

# The external issuer's JWKS: the public keys of
# framework/var/issuer-keys/issuer/, each a .jwks beside its .pem (see
# fed_key_make).
fed_write_issuer_jwks() {
  mkdir -p framework/var/generated/issuer
  python3 - framework/var/issuer-keys/issuer <<'EOF' \
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

fed_generate() {
  # Every service, dev included, gets its federation URLs from here. In
  # `tiny`, one host is everything.
  if [ "${fed_topology}" = tiny ]; then
    fed_discovery_url=https://fed:8444
    fed_director_url=https://fed:8444
    fed_registry_url=https://fed:8444
    directors=https://fed:8444
    fed_origin_url=https://fed:8444
    fed_origin_key_dir=issuer-keys/fed
  else
    fed_discovery_url=https://discovery:8444
    fed_director_url=https://director-0:8444
    fed_registry_url=https://registry:8444
    directors=https://director-0:8444
    fed_origin_url=https://origin-0:8444
    fed_origin_key_dir=issuer-keys/origin-0
  fi
  # The key that signs tokens the origins' exports accept.
  if [ "${EXTERNAL_ISSUER}" = true ]; then
    fed_origin_key_dir=issuer-keys/issuer
  fi
  fed_federation_url="pelican://${fed_discovery_url#https://}"
  fed_jwks_uri="${fed_director_url}/.well-known/issuer.jwks"

  {
    printf -- '---\n'
    printf '# Generated by %s. Do not edit.\n\n' "${progname}"
    printf 'Federation:\n'
    printf '  DiscoveryUrl: %s\n' "${fed_discovery_url}"
    printf '  DirectorUrl: %s\n' "${fed_director_url}"
    printf '  RegistryUrl: %s\n\n' "${fed_registry_url}"
    printf 'Server:\n  DirectorUrls:\n'
    for d in ${directors}; do printf '    - %s\n' "${d}"; done
  } | fed_write framework/var/generated/conf/50-topology.yaml

  # Knobs that nothing else depends on, so a later layer may override
  # them, e.g. for one service. Every server acts on the EnableOIDC flags
  # of the modules it runs.
  {
    printf -- '---\n'
    printf '# Generated by %s. Do not edit.\n\n' "${progname}"
    printf 'Logging:\n  Level: %s\n' "${PELICAN_LOG_LEVEL}"
    for module in Origin Cache Director Registry; do
      printf '\n%s:\n  EnableOIDC: %s\n' "${module}" "${ENABLE_OIDC}"
    done
  } | fed_write framework/var/generated/conf/50-knobs.yaml

  # The `discovery` service's documents (unused by `tiny`).
  {
    printf '{\n'
    printf '  "discovery_endpoint": "%s",\n' "${fed_discovery_url}"
    printf '  "director_endpoint": "%s",\n' "${fed_director_url}"
    printf '  "director_advertise_endpoints": [\n'
    first=1
    for d in ${directors}; do
      [ "${first}" -eq 1 ] || printf ',\n'
      first=0
      printf '    "%s"' "${d}"
    done
    printf '\n  ],\n'
    printf '  "namespace_registration_endpoint": "%s",\n' "${fed_registry_url}"
    printf '  "jwks_uri": "%s",\n' "${fed_jwks_uri}"
    printf '  "broker_endpoint": ""\n'
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

  # Read by test.py and init-data.py, which run in framework/var
  # (hence the relative paths). issuer-urls has the issuer URL the origins
  # give each namespace, or would give it: /public, which has no issuer,
  # and any namespace the shape does not export.
  for ns in public protected-a protected-b; do
    printf '%s %s\n' "${ns}" "$(fed_issuer_url "/${ns}")"
  done | fed_write framework/var/generated/issuer-urls
  # What fed.sh wrote before issuer-urls.
  rm -f framework/var/generated/origin-issuer-url \
      framework/var/generated/origin-issuer-url-public
  printf '%s\n' "${fed_origin_url}" | fed_write framework/var/generated/origin-web-url
  printf '%s\n' "${fed_origin_key_dir}" | fed_write framework/var/generated/origin-key-dir
  printf '%s\n' "${fed_federation_url}" | fed_write framework/var/generated/federation-url
  printf '%s\n' "${ORIGIN_VARIANT}" | fed_write framework/var/generated/origin-variant
  printf '%s\n' "${ORIGIN_POSC}" | fed_write framework/var/generated/origin-posc
  printf '%s\n' "${ORIGIN_CACHE_CONTROL}" \
      | fed_write framework/var/generated/origin-cache-control
  if [ "${ORIGIN_METADATA}" = true ]; then
    printf '%s\n' "${ORIGIN_METADATA_MODE}"
  else
    printf 'off\n'
  fi | fed_write framework/var/generated/origin-metadata

  # One line per exported namespace: its prefix and capabilities. The
  # tests read it to know what each namespace should allow.
  for ns in ${fed_namespaces}; do
    fed_namespace_caps "${ns}"
    printf '/%s %s\n' "${ns}" "$(printf '%s' "${fed_caps}" | tr ' ' ,)"
  done | fed_write framework/var/generated/exports

  # One line per running origin that exports these namespaces (so not
  # origin-2, another owner): service, the URL under which it serves its
  # exports, and the directory (under framework/var) that is its
  # StoragePrefix. A pstore origin's store is encrypted, so the directory
  # named is the plain copy that the transfers suite uploads to it (marked
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

  # Likewise for each running cache: service, data URL, and kind.
  {
    if [ "${fed_topology}" = tiny ]; then
      fed_cache_line fed
    else
      fed_cache_line cache-0
      if fed_has_profile multi-cache; then
        fed_cache_line cache-1
      fi
    fi
  } | fed_write framework/var/generated/caches

  # Every service's generated layer; see fed_instance_write.
  for svc in ${fed_services} dev; do
    mkdir -p "framework/var/generated/instance/${svc}"
  done

  # The origins' exports, of the namespaces in ORIGIN_NAMESPACES, in full:
  # a later layer replaces a list rather than merging into it. IssuerUrls
  # is pinned where the origins' own issuer must not be:
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
  # What fed.sh called the file before it also restated capabilities.
  rm -f framework/var/generated/conf/60-origin-issuers.yaml
  {
    fed_exports_header
    first=yes
    for ns in ${fed_namespaces}; do
      [ -n "${first}" ] || printf '\n'
      first=""
      pinned=""
      if [ "${pin_issuers}" = true ]; then
        pinned=$(fed_issuer_url "/${ns}")
      fi
      jwks="/fed/issuer-jwks/ns-${ns}.jwks"
      if [ "${EXTERNAL_ISSUER}" = true ]; then
        jwks=""
      fi
      fed_export_entry "/${ns}" "${ns}" "$(fed_storage_prefix "${ns}")" "${pinned}" "${jwks}"
    done
  } | fed_write framework/var/generated/conf/60-origin-exports.yaml

  # origin-2, another owner (`topo-multi-owner`), with an issuer and a key
  # of its own: /other is like /protected-a, and /public/other like
  # /public, but nested in it. It has nothing like /protected-b. Its layer
  # beats the shared one above. So that it can register a prefix inside
  # origin-0's with its own key, the registry does not require key
  # chaining.
  if fed_has_profile multi-owner; then
    {
      fed_exports_header
      first=yes
      for ns in ${fed_namespaces}; do
        case "${ns}" in
          public)      prefix=/public/other ;;
          protected-a) prefix=/other ;;
          *)           continue ;;
        esac
        [ -n "${first}" ] || printf '\n'
        first=""
        fed_export_entry "${prefix}" "${ns}" "$(fed_storage_prefix "${ns}")" "" ""
      done
    } | fed_instance_write origin-2 60-origin-exports.yaml
    {
      printf -- '---\n'
      printf '# Generated by %s. Do not edit.\n\n' "${progname}"
      printf 'Registry:\n  RequireKeyChaining: false\n'
    } | fed_write framework/var/generated/conf/60-registry.yaml
  else
    fed_instance_rm origin-2 60-origin-exports.yaml
    rm -f framework/var/generated/conf/60-registry.yaml
  fi

  # Each origin's backend (`origin-httpsv2`, `origin-s3v2`): its store,
  # framework/var/data/origin/<N>, is /srv/origin-<N> on the backend,
  # which the S3 server serves as bucket origin-<N>.
  for svc in origin-0 origin-1 origin-2; do
    case "${ORIGIN_VARIANT}" in
      httpsv2)
          {
            printf -- '---\n'
            printf '# Generated by %s. Do not edit.\n\n' "${progname}"
            printf 'Origin:\n  HttpServiceUrl: https://webdav:8444/%s\n' "${svc}"
          } | fed_instance_write "${svc}" 60-backend.yaml ;;
      s3v2)
          {
            printf -- '---\n'
            printf '# Generated by %s. Do not edit.\n\n' "${progname}"
            printf 'Origin:\n  S3Bucket: %s\n' "${svc}"
          } | fed_instance_write "${svc}" 60-backend.yaml ;;
      *)  fed_instance_rm "${svc}" 60-backend.yaml ;;
    esac
  done

  # posixv2 multiuser (`origin-multiuser`): each request's filesystem
  # operations run as the Unix user its token maps to, who must exist in
  # the origin's image. The tests' tokens map to `pelican` (uid 10941);
  # any other token, e.g. a director's test, to `xrootd` (10940); and
  # requests without a token run as `nobody`, Pelican's default.
  if [ "${ORIGIN_MULTIUSER}" = true ]; then
    {
      printf '[\n'
      printf '  {"sub": "pelican-test-framework", "result": "pelican",\n'
      printf '   "comment": "the tests'"'"' tokens (framework/testlib/credentials.py)"}\n'
      printf ']\n'
    } | fed_write framework/var/generated/multiuser-mapfile.json
    {
      printf -- '---\n'
      printf '# Generated by %s. Do not edit.\n\n' "${progname}"
      printf 'Origin:\n'
      printf '  ScitokensNameMapFile: /fed/generated/multiuser-mapfile.json\n'
      printf '  ScitokensDefaultUser: xrootd\n'
    } | fed_write framework/var/generated/conf/60-multiuser.yaml
  else
    rm -f framework/var/generated/multiuser-mapfile.json \
        framework/var/generated/conf/60-multiuser.yaml
  fi

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

  # The OIDC client (see framework/presets/auth-oidc.sh), and the S3
  # server's credentials (see framework/presets/origin-s3v2.sh).
  for f in oidc-client-id oidc-client-secret s3-access-key s3-secret-key; do
    if [ -f "local/etc/${f}" ]; then
      fed_write "framework/var/generated/${f}" <"local/etc/${f}"
    else
      fed_write "framework/var/generated/${f}" <"framework/etc/${f}"
    fi
  done
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
  mkdir -p \
      framework/var/data/origin/0 framework/var/data/origin/1 \
      framework/var/data/origin/2 \
      framework/var/data/cache/0 framework/var/data/cache/1 \
      framework/var/data/cache/fed framework/var/data/lab-server \
      framework/var/data/metadata framework/var/grafana \
      framework/var/issuer-jwks framework/var/test-keys
  # Written over SSH by `alice`, whose uid is unrelated to yours.
  chmod 0777 framework/var/data/lab-server
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
# Released binaries in framework/var/bin/, for use outside the service
# containers:
#
#   linux/    the client the tests use in the dev container
#   <host>/   pelican-server for `keys`, plus a client for the host
#
# On a Linux host these are the same directory.

fed_os=$(uname -s)
fed_arch=$(uname -m)
case "${fed_arch}" in
  aarch64) fed_arch=arm64 ;;
esac
fed_host_bin="framework/var/bin/$(printf '%s' "${fed_os}" | tr '[:upper:]' '[:lower:]')"

# Unpack binary $1 at version $2 for OS $3 into framework/var/bin/<os>.
fed_fetch_archive() {
  tarball="$1_$3_${fed_arch}.tar.gz"
  dir="framework/var/bin/$(printf '%s' "$3" | tr '[:upper:]' '[:lower:]')"
  mkdir -p "${dir}"
  ( cd "${dir}" || exit 1
    curl -fSL -O \
        "https://github.com/PelicanPlatform/pelican/releases/download/v$2/${tarball}"
    tar xzf "${tarball}"
    mv "$1-$2/$1" .
    rm -rf "${tarball}" "$1-$2" )
}

# True if framework/var/bin/ holds PELICAN_TAG's binaries. Only the
# host's pelican-server is version-checked, so a custom linux/pelican
# survives `init`.
fed_have_binaries() {
  [ -x framework/var/bin/linux/pelican ] || return 1
  [ -e framework/var/bin/linux/stash_plugin ] || return 1
  [ -x "${fed_host_bin}/pelican" ] || return 1
  [ -x "${fed_host_bin}/pelican-server" ] || return 1
  "${fed_host_bin}/pelican-server" --version 2>/dev/null \
      | grep -qxF "Version: ${PELICAN_TAG#v}" || return 1
  return 0
}

fed_fetch_binaries() {
  version=${PELICAN_TAG#v}
  printf 'Downloading the %s binaries ...\n' "${version}"

  # The client dispatches on its name; the transfers suite's plugin-*
  # scenarios invoke stash_plugin.
  fed_fetch_archive pelican "${version}" Linux
  ln -sf pelican framework/var/bin/linux/stash_plugin

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
# Pelican ignores the .jwks beside each key. `keys list` reads it, and
# the external issuer (`auth-external-issuer`) publishes its own.

fed_key_check_target() {
  case "$1" in
    origins|all) return 0 ;;
  esac
  for svc in ${fed_services} ${fed_key_others}; do
    if [ "${svc}" = "$1" ]; then
      return 0
    fi
  done
  die_usage "no such key target: $1" \
      "(try: ${fed_services} ${fed_key_others} origins all)"
}

# The directories that must hold the same key as keyset $1. Used in
# command substitutions, so it must not die.
# shellcheck disable=SC2086  # the lists are meant to be split
fed_key_dirs() {
  case "$1" in
    origins)   printf '%s ' ${fed_group_origins} ;;
    *)         printf '%s ' "$1" ;;
  esac
}

# The keysets that target $1 names. `all` is one key for the `origins`
# group plus one per other key holder, not one key for everything.
fed_key_sets() {
  case "$1" in
    all) printf 'origins'
         for svc in ${fed_services} ${fed_key_others}; do
           case " ${fed_group_origins} " in
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
    for key in "framework/var/issuer-keys/${dir}"/[0-9][0-9]-*.pem; do
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
  for dir in "$@"; do
    [ -w "framework/var/issuer-keys/${dir}" ] \
        || die "framework/var/issuer-keys/${dir}/ is not yours to write: under" \
               "SERVER_DROP_PRIVILEGES, Pelican gives it to the container's" \
               "pelican user. Start over with ./reset.sh"
  done

  # `key create` will not overwrite a .jwks left behind by a deleted .pem.
  if [ -e "framework/var/issuer-keys/${first}/${name}.jwks" ] \
      && [ ! -e "framework/var/issuer-keys/${first}/${name}.pem" ]; then
    rm -f "framework/var/issuer-keys/${first}/${name}.jwks"
  fi

  printf 'Creating framework/var/issuer-keys/%s/%s.pem ...\n' "${first}" "${name}"
  "${fed_host_bin}/pelican-server" key create \
      --private-key "framework/var/issuer-keys/${first}/${name}.pem" \
      --public-key "framework/var/issuer-keys/${first}/${name}.jwks" >/dev/null

  [ -f "framework/var/issuer-keys/${first}/${name}.pem" ] \
      || die "framework/var/issuer-keys/${first}/${name}.pem was not created"

  chmod 0640 "framework/var/issuer-keys/${first}/${name}.pem"
  chmod 0644 "framework/var/issuer-keys/${first}/${name}.jwks"

  shift
  for dir in "$@"; do
    printf 'Copying it to framework/var/issuer-keys/%s/ ...\n' "${dir}"
    cp "framework/var/issuer-keys/${first}/${name}.pem" "framework/var/issuer-keys/${dir}/${name}.pem"
    cp "framework/var/issuer-keys/${first}/${name}.jwks" "framework/var/issuer-keys/${dir}/${name}.jwks"
  done
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
      [ -e "${key}" ] || continue
      cp "${key}" "framework/var/issuer-keys/${dir}/"
    done
  done
}

# Every service gets a key, whether or not the current shape starts it.
fed_key_init() {
  fed_key_init_group origins

  for svc in ${fed_services} ${fed_key_others}; do
    case " ${fed_group_origins} " in
      *" ${svc} "*) continue ;;
    esac
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

# Test keys, for the credentials in framework/testlib/credentials.py. Like
# the issuer keys, they are created once and never replaced:
#
#   test-keys/jwks.pem        its public key is issuer-jwks/test.jwks,
#                             which the origins name in Server.IssuerJwks
#   test-keys/unknown.pem     its public key (unknown.jwks) goes nowhere
#   test-keys/ns-<ns>.pem     its public key is issuer-jwks/ns-<ns>.jwks,
#                             which the origins name in the IssuerJwks of
#                             the /<ns> export alone; only /protected-a
#                             and /protected-b have issuers, and so keys
#
# The origins mount issuer-jwks/ but not test-keys/, so no server holds
# any of the private keys.
fed_test_key_make() {  # $1 = private key, $2 = its public key (JWKS)
  if [ -f "$1" ] && [ -f "$2" ]; then
    return 0
  fi
  [ -x "${fed_host_bin}/pelican-server" ] \
      || die "${fed_host_bin}/pelican-server is missing;" \
             "run './${progname} init' first"

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

fed_key_new() {
  verb=$1
  target=${2:-}
  [ -n "${target}" ] \
      || die_usage "keys ${verb} needs a target" \
                   "(try: origins, all, or a service name)"
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
        kid=$(sed -n 's/.*"kid"[^"]*"\([^"]*\)".*/\1/p' "${key%.pem}.jwks" \
            | head -1)
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
      kid=$(sed -n 's/.*"kid"[^"]*"\([^"]*\)".*/\1/p' "${jwks}" | head -1)
    fi
    printf '  %-28s %s\n' "${key%%:*}.pem" "${kid}"
  done
  printf "The origins list jwks.pem's public key in Server.IssuerJwks, and\n"
  printf "ns-<ns>.pem's in the IssuerJwks of the /<ns> export alone;\n"
  printf 'no server knows unknown.pem.\n'
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
  if fed_has_profile multi-origin; then
    printf '%s\n' origin-1
  fi
  if fed_has_profile multi-owner; then
    printf '%s\n' origin-2
  fi
  if fed_has_profile multi-cache; then
    printf '%s\n' cache-1
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

fed_require_certs() {
  for cert in ${fed_certs}; do
    [ -f "${cert}" ] || die "${cert} is missing; run './${progname} init'"
  done
  if ! fed_certs_fresh; then
    warn "framework/var/certs/ has expired or expires within a day;" \
         "replace it with './${progname} init'"
  elif ! fed_certs_current; then
    warn "framework/etc/tls.req names hosts that the server certificate" \
         "does not; reissue it with './${progname} init'"
  fi
}

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

fed_require_lab_key() {
  fed_has_profile lab || return 0
  [ -s framework/var/generated/ssh/authorized_keys ] \
      || die "framework/var/generated/ssh/authorized_keys is empty;" \
             "run './${progname} init' to create the lab server's key pair"
}

# framework/init-data.py runs on the host (see README.md, Requirements).
fed_require_python() {
  command -v python3 >/dev/null 2>&1 \
      || die "python3 is missing; install Python 3.9 or later"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
      || die "python3 is $(python3 -V 2>&1 | cut -d' ' -f2); install Python 3.9 or later"
}

# The dev container mounts ~/.gitconfig. This is the only file the
# framework creates outside the checkout.
fed_require_gitconfig() {
  [ -e "${HOME}/.gitconfig" ] || : >"${HOME}/.gitconfig"
}

# Start the dev container, which only docker-compose.yaml defines,
# whatever the topology.
fed_dev_up() {
  fed_require_gitconfig
  COMPOSE_IGNORE_ORPHANS=true \
      docker compose -f framework/docker-compose.yaml up --detach dev
}

# Warn (only) if the test data was built for a different shape.
fed_check_data_shape() {
  want=$(./framework/init-data.py --print-shape)
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
  for path in framework/var/data/origin/0 framework/var/data/origin/1 \
      framework/var/data/origin/2 framework/var/data/cache/0 \
      framework/var/data/cache/1 framework/var/data/cache/fed; do
    chmod 0777 "${path}" 2>/dev/null \
        || warn "could not let the pelican user write ${path}"
  done
  if [ -f framework/var/certs/tls.key ]; then
    chmod 0444 framework/var/certs/tls.key 2>/dev/null \
        || warn "could not let the pelican user read framework/var/certs/tls.key"
  fi
}

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

  if [ -f framework/var/data/.init-shape ] \
      && [ "$(cat framework/var/data/.init-shape)" = "$(./framework/init-data.py --print-shape)" ]; then
    printf 'Keeping the test data in framework/var/data/, built for this shape.\n'
  else
    ./framework/init-data.py
  fi

  fed_prepare_unprivileged
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
# mounted file does. After framework/var/generated/conf/ or instance/
# changes (see fed_conf_sum), name
# the Pelican services still running with the old configuration (same
# container ID as before `up`). framework/var/generated/conf-changed
# holds those IDs until none are left.
fed_up() {
  fed_remove_unselected

  before=""
  if [ -e framework/var/generated/conf-changed ]; then
    before=$(cat framework/var/generated/conf-changed)
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

  # Recreated, not restarted: fed_up tracks stale configuration by
  # container ID.
  restart)
    fed_preflight
    fed_up --force-recreate "$@"
    ;;

  dev|test)
    fed_dev_up
    if [ "${cmd}" = test ]; then
      # RESULTS_TAG goes through, to name the results (see test.py).
      set -- env ${RESULTS_TAG:+"RESULTS_TAG=${RESULTS_TAG}"} /scratch/test.py "$@"
    fi
    [ $# -gt 0 ] || set -- bash -il
    # No TTY unless both ends are a terminal, so output can be captured.
    no_tty=-T
    if [ -t 0 ] && [ -t 1 ]; then
      no_tty=""
    fi
    exec docker compose -f framework/docker-compose.yaml \
        exec ${no_tty:+"${no_tty}"} dev "$@"
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
