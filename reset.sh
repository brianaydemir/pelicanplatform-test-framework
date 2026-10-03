#!/bin/sh
#
# Return the checkout to a fresh state. Needs a running Docker daemon.
#
#   ./reset.sh          stop everything; discard state, data, keys,
#                       binaries, certificates, and remembered presets
#
# Everything discarded is in framework/var/. local/ and
# framework/var/grafana/ are left alone. Afterward, set up again with
# `./fed.sh init`.

set -eu

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) die_usage "takes no arguments" ;;
esac

# Read before framework/var/generated/ goes away.
presets=""
if [ -f framework/var/generated/presets ]; then
  presets=$(cat framework/var/generated/presets)
fi

# `-p default`: the full topology's file declares the dev container's
# volumes, and a broken remembered preset can't stop the teardown.
printf 'Stopping the federation ...\n'
./fed.sh -p default down --volumes || true

# A teardown that failed would leave containers that the next `up` keeps,
# still bound to what is discarded below.
left=$(docker ps --all --quiet --filter label=com.docker.compose.project=pelican) || {
  printf '%s: cannot list the containers left behind\n' "${progname}" >&2
  exit 1
}
if [ -n "${left}" ]; then
  {
    printf '%s: the federation did not stop; these containers remain:\n' "${progname}"
    # shellcheck disable=SC2086  # one ID per word
    docker inspect --format '  {{.Name}}' ${left} | sed 's|  /|  |'
    printf '  Stop them with: docker rm -f %s\n' "$(printf '%s' "${left}" | tr '\n' ' ')"
  } >&2
  exit 1
fi

# Everything in framework/var/ but Grafana's database. Keys, state, and
# data go together: stores and backups are sealed to the keys.
set --
for path in framework/var/* framework/var/.[!.]*; do
  [ -e "${path}" ] || continue
  [ "${path}" != framework/var/grafana ] || continue
  set -- "$@" "${path}"
done

all_gone() {
  for path in "$@"; do
    [ ! -e "${path}" ] || return 1
  done
  return 0
}

printf 'Discarding the data, state, keys, binaries, and certificates ...\n'
if [ $# -gt 0 ]; then
  rm -rf -- "$@" 2>/dev/null || true
fi

# On a Linux host, containers leave root-owned files behind. Remove them
# from a container.
if ! all_gone "$@"; then
  printf 'Removing the rest as root, from inside a container ...\n'
  docker run --rm --pull=missing --network none --user 0:0 \
      --volume "${PWD}:/w" --workdir /w --entrypoint /bin/rm \
      nginx:alpine -rf -- "$@" || true
fi

if [ -n "${presets}" ]; then
  printf 'Discarded the remembered presets: %s\n' "${presets}"
fi

if ! all_gone "$@"; then
  {
    printf '%s: some files could not be removed.\n' "${progname}"
    printf '  Remove the rest with: sudo rm -rf --'
    for path in "$@"; do
      [ ! -e "${path}" ] || printf ' %s' "${path}"
    done
    printf '\n'
  } >&2
  exit 1
fi
