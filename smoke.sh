#!/bin/sh
#
# Run Pelican's smoke tests over a matrix of shapes: for each shape, start
# from scratch, start the federation, run the tests (./fed.sh test, which
# runs every suite that applies), and stop.
#
#   ./smoke.sh              every shape
#   ./smoke.sh ssh tiny     only these shapes
#   ./smoke.sh -o DIR ...   put logs and results in DIR
#   ./smoke.sh -l           list the shapes and what each runs
#   ./smoke.sh --names      list only the shapes' names, one per line
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

all_shapes="default xrootd multi xrootd-multi ssh pstore tiny tiny-xrootd"
all_shapes="${all_shapes} extras posc metadata metadata-tx"

shape_presets() {
  case "$1" in
    default)         echo "-p default" ;;
    xrootd)          echo "-p origin-xrootd -p cache-xrootd" ;;
    multi)           echo "-p topo-multi-origin -p topo-multi-cache" ;;
    xrootd-multi)    echo "-p origin-xrootd -p cache-xrootd" \
                          "-p topo-multi-origin -p topo-multi-cache" ;;
    ssh)             echo "-p origin-ssh" ;;
    pstore)          echo "-p origin-pstore" ;;
    tiny)            echo "-p topo-tiny" ;;
    tiny-xrootd)     echo "-p topo-tiny -p origin-xrootd" ;;
    extras)          echo "-p with-grafana -p with-lab -p auth-oidc -p origin-max-age" ;;
    posc)            echo "-p origin-posc" ;;
    metadata)        echo "-p origin-posc -p origin-metadata" ;;
    metadata-tx)     echo "-p origin-metadata-tx" ;;
    *)               return 1 ;;
  esac
}

shape_notes() {
  case "$1" in
    default)     echo "+ ports; then rotates every key and reruns transfers, auth" ;;
    extras)      echo "+ Grafana" ;;
    metadata-tx) echo "metadata only" ;;
  esac
}

log_dir=${SMOKE_LOG_DIR:-}
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -l|--list)
      printf 'Every shape runs every suite that applies to it, except as noted.\n\n'
      for shape in ${all_shapes}; do
        printf '  %-16s %s\n' "${shape}" "$(shape_presets "${shape}")"
        notes=$(shape_notes "${shape}")
        [ -z "${notes}" ] || printf '  %-16s   %s\n' "" "${notes}"
      done
      exit 0 ;;
    --names)
      for shape in ${all_shapes}; do
        printf '%s\n' "${shape}"
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
# recorded as <suite>-<tag> (see RESULTS_TAG in framework/testlib/report.py).
tests()         { ./fed.sh test; }
tests_rotated() { RESULTS_TAG=rotated-keys ./fed.sh test transfers auth; }
metadata()      { ./fed.sh test metadata; }

# Every published port answers over TLS with the framework's CA.
check_ports() {
  for url in \
      https://localhost:8444/api/v1.0/health \
      https://localhost:8445/api/v1.0/health \
      https://localhost:9000/api/v1.0/health \
      https://localhost:9001/api/v1.0/health \
      https://localhost:9005/.well-known/pelican-configuration; do
    printf '%s -> ' "${url}"
    curl -sS --max-time 10 --cacert framework/var/certs/ca.crt \
        -o /dev/null -w '%{http_code}\n' "${url}" || return 1
  done
}

check_grafana() {
  n=0
  until curl -fsS --max-time 5 -o /dev/null http://localhost:9003/api/health; do
    n=$(( n + 1 ))
    [ "${n}" -lt 30 ] || return 1
    sleep 2
  done
}

# What the shape runs after `up`. The tests run every suite that applies
# to the shape, e.g. posc under `-p origin-posc` or `-p origin-pstore`.
shape_checks() {
  case "$1" in
    default)
      step "ports" check_ports || return 1
      step "tests" tests
      rc=$?
      if step "keys rotate all" ./fed.sh keys rotate all && step "restart" restart; then
        step "tests (rotated keys)" tests_rotated || rc=1
      else
        rc=1
      fi
      return "${rc}" ;;
    extras)
      step "grafana" check_grafana || return 1
      step "tests" tests ;;
    metadata-tx)
      step "metadata" metadata ;;
    *)
      step "tests" tests ;;
  esac
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
  # shellcheck disable=SC2046  # one preset flag per word
  set -- $(shape_presets "${shape}")
  printf '\n%s (%s)\n' "${shape}" "$*"

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

  note=""
  # Why the transfers suite failed, if every download bypassed the caches.
  if grep -q 'no transfer went through a cache' "${log}"; then
    note="; no transfer went through a cache"
  fi
  summary="${summary}$(printf '%-16s %s (%ss%s)' "${shape}" "${result}" \
      "$(( $(date +%s) - start ))" "${note}")
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
