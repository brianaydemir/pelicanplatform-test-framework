#!/bin/sh
#
# Run Pelican's smoke tests over a matrix of shapes: for each shape, start
# from scratch, start the federation, run the tests (./fed.sh test, which
# runs every suite that applies), and stop.
#
#   ./smoke.sh              every shape
#   ./smoke.sh ssh-v2 tiny-pstore
#                           only these shapes
#   ./smoke.sh -o DIR ...   put logs and results in DIR
#   ./smoke.sh -l           list the shapes and what each runs
#   ./smoke.sh --names      list only the shapes' names, one per line
#   ./smoke.sh --table      list them for framework/matrix.py
#
# Together, the shapes make every pair of choices that the presets offer
# (see framework/matrix.py, which checks that they do). Some shapes also
# rotate every key, restart, and run the tests that use keys again.
#
# DESTRUCTIVE: runs ./reset.sh before each shape, keeping only
# framework/var/bin/, and leaves the last shape's state and presets
# behind. local/ and your environment apply as usual, so your images and
# client are what get tested. Only the shapes pick the topology,
# variants, and features.
#
# Logs and results go to DIR (default: $SMOKE_LOG_DIR, or
# smoke-logs/<timestamp>): <shape>.log, results/<shape>/*.tsv, and, merged
# over every shape, results.tsv, junit.xml, and summary.md. Exits non-zero
# if any shape fails.

set -u

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

all_shapes="default posixv2-xrootd-tx tiny-posixv2-posc ssh-xrootd s3v2-xrootd"
all_shapes="${all_shapes} tiny-xrootd pstore-xrootd s3v2-v2 tiny-posixv2-tx"
all_shapes="${all_shapes} posixv2-xrootd-multiuser httpsv2-xrootd tiny-pstore"
all_shapes="${all_shapes} posixv2-no-direct posixv2-xrootd-posc ssh-v2 httpsv2-v2"
all_shapes="${all_shapes} xrootd-xrootd"

# Each shape's presets, one per word.
shape_presets() {
  case "$1" in
    default)
      echo "topo-basic origin-posixv2 cache-v2" ;;
    posixv2-xrootd-tx)
      echo "cache-xrootd topo-multi-origin topo-multi-cache topo-multi-owner" \
           "auth-external-issuer origin-metadata-tx server-unprivileged" ;;
    tiny-posixv2-posc)
      echo "topo-tiny origin-posc origin-metadata server-unprivileged" ;;
    ssh-xrootd)
      echo "origin-ssh cache-xrootd topo-multi-cache" ;;
    s3v2-xrootd)
      echo "origin-s3v2 cache-xrootd topo-multi-owner origin-no-direct" \
           "server-unprivileged" ;;
    tiny-xrootd)
      echo "topo-tiny origin-xrootd origin-no-direct origin-max-age" \
           "server-unprivileged" ;;
    pstore-xrootd)
      echo "origin-pstore cache-xrootd topo-multi-origin topo-multi-cache" \
           "auth-external-issuer" ;;
    s3v2-v2)
      echo "origin-s3v2 topo-multi-origin topo-multi-cache auth-external-issuer" \
           "origin-max-age" ;;
    tiny-posixv2-tx)
      echo "topo-tiny origin-posc origin-metadata-tx origin-max-age" \
           "origin-multiuser" ;;
    posixv2-xrootd-multiuser)
      echo "cache-xrootd topo-multi-origin topo-multi-cache topo-multi-owner" \
           "auth-external-issuer origin-metadata origin-max-age origin-multiuser" ;;
    httpsv2-xrootd)
      echo "origin-httpsv2 cache-xrootd auth-external-issuer origin-no-direct" ;;
    tiny-pstore)
      echo "topo-tiny origin-pstore origin-max-age server-unprivileged" ;;
    posixv2-no-direct)
      echo "topo-multi-origin topo-multi-cache origin-no-direct" ;;
    posixv2-xrootd-posc)
      echo "cache-xrootd topo-multi-origin topo-multi-cache topo-multi-owner" \
           "auth-external-issuer origin-posc origin-multiuser" ;;
    ssh-v2)
      echo "origin-ssh topo-multi-origin auth-external-issuer origin-no-direct" \
           "origin-max-age" ;;
    httpsv2-v2)
      echo "origin-httpsv2 topo-multi-origin topo-multi-cache topo-multi-owner" \
           "origin-max-age server-unprivileged with-lab" ;;
    xrootd-xrootd)
      echo "origin-xrootd cache-xrootd topo-multi-origin topo-multi-cache" \
           "topo-multi-owner auth-external-issuer" ;;
    *)
      return 1 ;;
  esac
}

# Whether the shape rotates every key after the tests, then restarts and
# runs the tests that use keys again.
shape_rotates() {
  case "$1" in
    default|ssh-xrootd|pstore-xrootd|s3v2-v2|tiny-posixv2-tx) return 0 ;;
    posixv2-xrootd-multiuser|httpsv2-xrootd|xrootd-xrootd)    return 0 ;;
  esac
  return 1
}

shape_notes() {
  if shape_rotates "$1"; then
    printf 'then rotates every key and reruns %s\n' "${rotated_suites}"
  fi
}

# The suites that use keys, which a shape that rotates them runs again.
rotated_suites="owners auth transfers"

log_dir=${SMOKE_LOG_DIR:-}
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -l|--list)
      printf 'Every shape runs every suite that applies to it.\n\n'
      for shape in ${all_shapes}; do
        printf '  %s\n' "${shape}"
        printf '    %s\n' "$(shape_presets "${shape}")" | fold -s -w 76 \
            | sed -e 's/ *$//' -e '1!s/^/    /'
        notes=$(shape_notes "${shape}")
        [ -z "${notes}" ] || printf '    %s\n' "${notes}"
      done
      exit 0 ;;
    --names)
      for shape in ${all_shapes}; do
        printf '%s\n' "${shape}"
      done
      exit 0 ;;
    --table)
      for shape in ${all_shapes}; do
        rotates=no
        if shape_rotates "${shape}"; then
          rotates=yes
        fi
        printf '%s\t%s\t%s\n' "${shape}" "${rotates}" "$(shape_presets "${shape}")"
      done
      exit 0 ;;
    -o) [ $# -ge 2 ] || die_usage "-o needs a directory"
        log_dir=$2; shift 2 ;;
    -o*) log_dir=${1#-o}; shift ;;
    --) shift; break ;;
    -*) die_usage "unknown option: $1" ;;
    *) break ;;
  esac
done

shapes=${*:-${all_shapes}}
for shape in ${shapes}; do
  shape_presets "${shape}" >/dev/null \
      || die_usage "no such shape: ${shape} (try: ${all_shapes})"
done

for cmd in docker curl python3; do
  command -v "${cmd}" >/dev/null 2>&1 || die "${cmd} is not in PATH"
done
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon"

log_dir=${log_dir:-smoke-logs/$(date +%Y%m%d-%H%M%S)}
mkdir -p "${log_dir}/results" || exit 1

#---------------------------------------------------------------------------
# Helpers

# Record a row of the shape's own results (see framework/testlib/report.py).
# $1 is the step, $2 its status, $3 its seconds.
record() {
  if [ ! -f "${steps}" ]; then
    printf 'suite\tcase\tstatus\tseconds\tnote\n' >"${steps}"
  fi
  printf 'smoke\t%s\t%s\t%s\t\n' "$1" "$2" "$3" >>"${steps}"
}

# Run a step, appending its output to ${log}. $1 is the label.
step() {
  label=$1; shift
  printf '  %-24s ' "${label}"
  printf '\n==> %s: %s\n' "${label}" "$*" >>"${log}"
  began=$(date +%s)
  if "$@" >>"${log}" 2>&1; then
    record "${label}" PASS "$(( $(date +%s) - began ))"
    printf 'ok\n'
    return 0
  fi
  record "${label}" FAIL "$(( $(date +%s) - began ))"
  printf 'FAILED\n'
  return 1
}

# ./reset.sh, parking framework/var/bin/ in ${log_dir} meanwhile.
parked="${log_dir}/.bin"
unpark() {
  if [ -d "${parked}" ]; then
    mkdir -p framework/var && mv "${parked}" framework/var/bin
  fi
}
fresh_start() {
  if [ -d framework/var/bin ]; then
    mv framework/var/bin "${parked}" || return 1
  fi
  ./reset.sh
  rc=$?
  unpark || return 1
  return "${rc}"
}
trap 'unpark; exit 130' INT
trap 'unpark; exit 143' TERM

up()      { ./fed.sh up --wait --wait-timeout 300; }
restart() { ./fed.sh restart --wait --wait-timeout 300; }

# The tests, in the dev container (see test.py). A rerun's suites are
# recorded as <suite>-<tag> (see RESULTS_TAG in
# framework/testlib/report.py).
tests()         { ./fed.sh test; }
# shellcheck disable=SC2086  # one suite per word
tests_rotated() { RESULTS_TAG=rotated-keys ./fed.sh test ${rotated_suites}; }

# Every Pelican server, and the discovery host, answers on its published
# port over TLS with the framework's CA.
check_ports() {
  services=$(./fed.sh ps --services) || return 1
  checked=""
  for svc in ${services}; do
    case "${svc}" in
      fed|director-*|registry|origin-*|cache-*) path=/api/v1.0/health ;;
      discovery) path=/.well-known/pelican-configuration ;;
      *) continue ;;
    esac
    published=$(./fed.sh port "${svc}" 8444) || return 1
    url="https://localhost:${published##*:}${path}"
    printf '%s (%s) -> ' "${url}" "${svc}"
    code=$(curl -sS --max-time 10 --cacert framework/var/certs/ca.crt \
        -o /dev/null -w '%{http_code}' "${url}") || return 1
    printf '%s\n' "${code}"
    [ "${code}" = 200 ] || return 1
    checked=yes
  done
  [ -n "${checked}" ]
}

# What the shape runs after `up`: the tests run every suite that applies
# to the shape. A shape that rotates every key then restarts, so that
# every server rereads its keys, and runs the suites that use them again.
shape_checks() {
  step "ports" check_ports || return 1
  step "tests" tests
  rc=$?
  shape_rotates "$1" || return "${rc}"
  if step "keys rotate all" ./fed.sh keys rotate all && step "restart" restart; then
    step "tests (rotated keys)" tests_rotated || rc=1
  else
    rc=1
  fi
  return "${rc}"
}

# Keep the shape's results (framework/var/results/, which the next reset
# discards) beside its steps'.
save_results() {
  for tsv in framework/var/results/*.tsv; do
    [ -e "${tsv}" ] || continue
    cp "${tsv}" "${log_dir}/results/${shape}/"
  done
}

#---------------------------------------------------------------------------
# Main

printf 'Logs and results: %s/\n' "${log_dir}"

summary=""
failed=0
for shape in ${shapes}; do
  log="${log_dir}/${shape}.log"
  steps="${log_dir}/results/${shape}/smoke.tsv"
  rm -rf "${log_dir}/results/${shape}"
  mkdir -p "${log_dir}/results/${shape}"
  start=$(date +%s)
  set --
  for preset in $(shape_presets "${shape}"); do
    set -- "$@" -p "${preset}"
  done
  printf '\n%s (%s)\n' "${shape}" "$(shape_presets "${shape}")"

  result=PASS
  fresh=""
  if ! { step "reset" fresh_start \
           && fresh=yes \
           && step "init" ./fed.sh "$@" init \
           && step "status" ./fed.sh status \
           && step "up" up \
           && shape_checks "${shape}"; }; then
    result=FAIL
    failed=$(( failed + 1 ))
    ./fed.sh ps --all >"${log_dir}/${shape}.compose.log" 2>&1
    ./fed.sh logs --no-color --timestamps >>"${log_dir}/${shape}.compose.log" 2>&1
  fi
  # After a failed reset, framework/var/results/ may be another shape's.
  if [ -n "${fresh}" ]; then
    save_results
  fi
  step "down" ./fed.sh down || true

  summary="${summary}$(printf '%-26s %s (%ss)' "${shape}" "${result}" \
      "$(( $(date +%s) - start ))")
"
done

# One table over every shape, and its conversions for CI. Each shape run
# here replaced its own results; another's, from an earlier run into the
# same ${log_dir}, are merged too.
report_ok=yes
python3 framework/report.py merge "${log_dir}/results"/*/ >"${log_dir}/results.tsv" \
    && python3 framework/report.py junit "${log_dir}/results.tsv" >"${log_dir}/junit.xml" \
    && python3 framework/report.py markdown "${log_dir}/results.tsv" >"${log_dir}/summary.md" \
    || report_ok=""

printf '\n%s' "${summary}"
printf 'Logs and results: %s/\n' "${log_dir}"
if [ -z "${report_ok}" ]; then
  printf '%s: could not write %s/results.tsv, junit.xml, or summary.md\n' \
      "${progname}" "${log_dir}" >&2
  exit 1
fi
[ "${failed}" -eq 0 ]
