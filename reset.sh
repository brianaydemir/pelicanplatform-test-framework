#!/bin/sh
#
# Return the checkout to a fresh state. Needs a running Docker daemon.
#
#   ./reset.sh          stop everything; discard state, data, keys,
#                       binaries, and certificates
#   ./reset.sh init     ... then `./fed.sh init` with the same presets
#
# grafana/ and your local config files are left alone.

set -eu

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

cmd="${1:-}"
case "${cmd}" in
  -h|--help) usage; exit 0 ;;
  ""|init) ;;
  *) die_usage "takes only init" ;;
esac

# Read before generated/ goes away.
presets=""
if [ -f generated/presets ]; then
  presets=$(cat generated/presets)
fi

# The full topology's file declares the dev container's volumes.
printf 'Stopping the federation ...\n'
TOPOLOGY=full ./fed.sh down --volumes || true

# Keys, state, and data go together: stores and backups are sealed to the
# keys.
set -- .condor_creds data generated issuer-keys/*/ state \
    ./bin/*/ certs/*.crt certs/*.csr certs/*.key certs/*.srl

all_gone() {
  for path in "$@"; do
    [ ! -e "${path}" ] || return 1
  done
  return 0
}

printf 'Discarding the data, state, keys, binaries, and certificates ...\n'
rm -rf -- "$@" 2>/dev/null || true

# On a Linux host, containers leave root-owned files behind. Remove them
# from a container.
if ! all_gone "$@"; then
  printf 'Removing the rest as root, from inside a container ...\n'
  docker run --rm --pull=missing --network none --user 0:0 \
      --volume "${PWD}:/w" --workdir /w --entrypoint /bin/rm \
      nginx:alpine -rf -- "$@" || true
fi

if ! all_gone "$@"; then
  {
    printf '%s: some files could not be removed.\n' "${progname}"
    printf '  Remove the rest with: sudo rm -rf --'
    for path in "$@"; do
      [ ! -e "${path}" ] || printf ' %s' "${path}"
    done
    printf '\n'
    if [ "${cmd}" = init ]; then
      flags=""
      for preset in ${presets}; do
        flags="${flags} -p ${preset}"
      done
      printf '  Then run: ./fed.sh%s init\n' "${flags}"
    fi
  } >&2
  exit 1
fi

if [ "${cmd}" = init ]; then
  set --
  for preset in ${presets}; do
    set -- "$@" -p "${preset}"
  done
  ./fed.sh "$@" init
fi
