#!/bin/sh
#
# Run the test load: one `stash_plugin` invocation per transfer ad in
# data/ads/input, MAX_CONCURRENT_ADS (default 32) at a time. Result ads
# go to data/ads/output, client logs to data/ads/log, and a summary is
# printed at the end. Exits non-zero if any scenario has unexpected
# results.
#
# Run inside the dev container:
#
#   ./fed.sh dev
#   cd /scratch && ./run.sh

set -u

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) die_usage "takes no arguments; set MAX_CONCURRENT_ADS in the environment" ;;
esac

bindir="bin/$(uname -s | tr '[:upper:]' '[:lower:]')"

max_concurrent=${MAX_CONCURRENT_ADS:-32}
case "${max_concurrent}" in
  ''|*[!0-9]*|0) die_usage "MAX_CONCURRENT_ADS must be a positive integer," \
                           "not '${max_concurrent}'" ;;
esac

#---------------------------------------------------------------------------
# Check prerequisites before the (possibly long) wait below.

[ -x "${bindir}/pelican" ] \
    || die "${bindir}/pelican is missing; run ./fed.sh init on the host"
[ -e "${bindir}/stash_plugin" ] \
    || die "${bindir}/stash_plugin is missing; run ./fed.sh init on the host"

# Derived by fed.sh from the current shape.
for f in federation-url origin-key-dir origin-issuer-url; do
  [ -f "generated/${f}" ] \
      || die "generated/${f} is missing; run ./fed.sh init"
done

federation=$(cat generated/federation-url)
key_dir=$(cat generated/origin-key-dir)
issuer=$(cat generated/origin-issuer-url)

# Sign with the origin's active key: the .pem that sorts first.
set -- "${key_dir}"/*.pem
origin_key=$1
[ -e "${origin_key}" ] || die "no key in ${key_dir}/; run ./fed.sh init"

total=0
for ad in data/ads/input/*; do
  [ -e "${ad}" ] || die "no transfer ads in data/ads/input; run ./fed.sh init"
  total=$(( total + 1 ))
done

# The client tries every token in .condor_creds, so clear old ones.
rm -rf data/ads/output data/ads/log ./.condor_creds
mkdir -p data/ads/output data/ads/log ./.condor_creds

#---------------------------------------------------------------------------
# Wait for the federation to serve an object. A new cache returns 403s
# until it learns the origin's auth configuration.

attempts=20
interval=15

printf 'Waiting for %s/public/data/0.0 ...\n' "${federation}"

n=1
while ! "${bindir}/pelican" object get \
    "${federation}/public/data/0.0" /dev/null; do
  if [ "${n}" -ge "${attempts}" ]; then
    {
      printf '\nGave up after %s attempts, %ss apart.\n' \
          "${attempts}" "${interval}"
      printf 'Is the federation up (./fed.sh status)? Was the origin primed?\n'
      printf "(A 'pstore' origin is empty until you upload to it.)\n"
    } >&2
    exit 1
  fi
  n=$(( n + 1 ))
  sleep "${interval}"
done

#---------------------------------------------------------------------------
# Mint the token for the `token` scenario. The client finds it in
# ./.condor_creds. It is never re-minted, so make it outlast the run.

token_lifetime=14400

printf 'Creating a token for %s/private/, good for %ss ...\n' \
    "${federation}" "${token_lifetime}"
"${bindir}/pelican" token create "${federation}/private/" \
    --read --scope-path / --subject pelican-test-framework \
    --issuer "${issuer}" --lifetime "${token_lifetime}" \
    --private-key "${origin_key}" \
    >.condor_creds/run.use \
    || die "could not create a token; is ${origin_key} the origin's key?"

#---------------------------------------------------------------------------
# Run the transfers. When the window is full, wait for the oldest one.

printf '\nStarting %s transfers, %s at a time ...\n\n' \
    "${total}" "${max_concurrent}"

started=0
running=0
pids=""

for ad in data/ads/input/*; do
  name=$(basename -- "${ad}")
  "${bindir}/stash_plugin" \
      -infile "${ad}" -outfile "data/ads/output/${name}" \
      2>"data/ads/log/${name}" &
  pids="${pids:+${pids} }$!"
  started=$(( started + 1 ))
  running=$(( running + 1 ))

  if [ "${running}" -ge "${max_concurrent}" ]; then
    case "${pids}" in
      *' '*) oldest=${pids%% *}; pids=${pids#* } ;;
      *)     oldest=${pids};     pids="" ;;
    esac
    wait "${oldest}"
    running=$(( running - 1 ))
  fi

  if [ $(( started % 100 )) -eq 0 ]; then
    printf '  started %s of %s\n' "${started}" "${total}"
  fi
done

printf '\nWaiting for the last transfers ...\n'
wait

#---------------------------------------------------------------------------
# Summarize. An ad passes if every transfer in it succeeded; a missing
# output file counts as a failure. `exist` and `token` should all pass,
# and `dne` should all fail.

status=0
printf '\n%-8s %8s %8s   %s\n' scenario passed failed expected
for scenario in exist token dne; do
  passed=0
  failed=0
  for ad in data/ads/input/*."${scenario}"; do
    [ -e "${ad}" ] || continue
    out="data/ads/output/$(basename -- "${ad}")"
    if grep -q 'TransferSuccess = true' "${out}" 2>/dev/null \
        && ! grep -q 'TransferSuccess = false' "${out}"; then
      passed=$(( passed + 1 ))
    else
      failed=$(( failed + 1 ))
    fi
  done
  case "${scenario}" in
    dne) expected="all fail";  [ "${passed}" -eq 0 ] || status=1 ;;
    *)   expected="all pass";  [ "${failed}" -eq 0 ] || status=1 ;;
  esac
  printf '%-8s %8s %8s   %s\n' "${scenario}" "${passed}" "${failed}" "${expected}"
done

# Which servers handled the transfers. A director that routes around its
# caches still passes every scenario, so make that visible.
printf '\nServed by (all attempts):\n'
served=$(cat data/ads/output/* 2>/dev/null \
    | grep -o 'Endpoint[0-9]* = "[^":]*' | sed 's/.*"//' | sort | uniq -c)
printf '%s\n' "${served:-  (none recorded)}"
case "${federation}" in
  pelican://fed:*) ;;  # `tiny`: one host is everything
  *)
    if ! printf '%s\n' "${served}" | grep -q ' cache-'; then
      printf '%s: warning: no transfer went through a cache\n' "${progname}" >&2
    fi ;;
esac

if [ "${status}" -eq 0 ]; then
  printf '\nOK. Results are in data/ads/output and data/ads/log.\n'
else
  printf '\nUNEXPECTED RESULTS. See data/ads/output and data/ads/log.\n' >&2
fi
exit "${status}"
