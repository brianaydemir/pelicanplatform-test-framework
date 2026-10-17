#!/bin/sh
#
# Run Pelican's smoke tests over a matrix of shapes: for each shape, start
# from scratch, start the federation, run the tests (./fed.sh test, which
# runs every suite that applies), and stop.
#
#   ./smoke.sh              every shape
#   ./smoke.sh default ssh-v2
#                           only these shapes
#   ./smoke.sh -o DIR ...   put logs and results in DIR
#   ./smoke.sh -l           list the shapes and what each runs
#   ./smoke.sh --names      list only the shapes' names, one per line
#   ./smoke.sh --table      list them for framework/matrix.py
#
# Named no shapes, it runs those in framework/smoke-shapes.tsv, which
# framework/matrix.py generates: two baselines, default, which makes only
# the framework's own choices, and pelican-defaults, only those that
# Pelican makes when told nothing; and as many other shapes as are
# useful, each as simple as it can be. Together, they make every pair of
# choices that the presets offer, but those that would test nothing, or
# nothing that other shapes don't (see "The smoke matrix" in
# docs/development.md). Some shapes also rotate every key, restart, and
# run every suite again.
#
# DESTRUCTIVE: runs ./reset.sh before each shape, keeping only
# framework/var/bin/, and leaves the last shape's state and presets
# behind. local/ and your environment apply as usual, so your images and
# client are what get tested. Only the shapes pick the topology,
# variants, and features.
#
# Logs and results go to DIR (default: $SMOKE_LOG_DIR, or
# smoke-logs/<timestamp>): <shape>.log, results/<shape>/*.tsv, the
# services' logs from each run of the tests, logs/<shape>/<time>/, and,
# merged over every shape, results.tsv, junit.xml, and summary.md. A
# shape that fails also leaves <shape>.compose.log, every service's log
# when it failed. Exits non-zero if any shape fails.

set -u

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

# The shapes, one per line of framework/smoke-shapes.tsv, but for its
# comments: each one's name, whether it rotates every key (yes or no),
# and its presets, separated by tabs. framework/matrix.py writes it.
shapes_file=framework/smoke-shapes.tsv
[ -r "${shapes_file}" ] || die "cannot read ${shapes_file}"
shape_rows() { grep -v -e '^#' -e '^[[:space:]]*$' "${shapes_file}"; }
all_shapes=$(shape_rows | cut -f 1 | tr '\n' ' ')

# Each shape's presets, one per word; fails if there is no such shape.
shape_presets() {
  shape_rows | awk -F '\t' -v shape="$1" \
      '$1 == shape { print $3; found = 1 } END { exit !found }'
}

# Whether the shape rotates every key after the tests, then restarts and
# runs every suite again.
shape_rotates() {
  shape_rows | awk -F '\t' -v shape="$1" \
      '$1 == shape && $2 == "yes" { found = 1 } END { exit !found }'
}

shape_notes() {
  if shape_rotates "$1"; then
    printf 'then rotates every key, restarts, and reruns every suite\n'
  fi
}

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
      shape_rows
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
      || die_usage "no such shape: ${shape} (see ./${progname} --names)"
done

for cmd in docker curl python3; do
  command -v "${cmd}" >/dev/null 2>&1 || die "${cmd} is not in PATH"
done
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon"

log_dir=${log_dir:-smoke-logs/$(date +%Y%m%d-%H%M%S)}
mkdir -p "${log_dir}/results" || exit 1
# Left by a run that was killed before it could put framework/var/bin/
# back. Parking another there would test that run's client.
[ ! -e "${log_dir}/.bin" ] \
    || die "${log_dir}/.bin is framework/var/bin/ from a run that was killed;" \
           "move it back to framework/var/bin/, or remove it"

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
  if [ -e "${parked}" ]; then
    printf '%s is in the way of parking framework/var/bin/\n' "${parked}"
    return 1
  fi
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
# framework/testlib/report.py), beside the first run's.
tests()         { ./fed.sh test; }
tests_rotated() { RESULTS_TAG=rotated-keys ./fed.sh test; }

# Every service is still running (none is meant to exit), and every
# Pelican server, and the discovery host, answers on its published port
# over TLS with the framework's CA.
check_ports() {
  states=$(./fed.sh ps --all --format '{{.Service}} {{.State}}') || return 1
  printf '%s\n' "${states}"
  stopped=$(printf '%s\n' "${states}" | awk 'NF && $2 != "running" { print $1 }')
  if [ -n "${stopped}" ]; then
    printf 'not running: %s\n' "$(printf '%s' "${stopped}" | tr '\n' ' ')"
    return 1
  fi
  services=$(printf '%s\n' "${states}" | awk 'NF { print $1 }')
  checked=""
  for svc in ${services}; do
    case "${svc}" in
      fed|director|director-*|registry|origin-*|cache-*|transfer) path=/api/v1.0/health ;;
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
# every server rereads its keys, and runs every suite again.
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
# discards) beside its steps', and the services' logs from each run of
# the tests (framework/var/logs/<time>/; see fed.sh's fed_save_logs).
save_results() {
  for tsv in framework/var/results/*.tsv; do
    [ -e "${tsv}" ] || continue
    cp "${tsv}" "${log_dir}/results/${shape}/"
  done
  if [ -d framework/var/logs ]; then
    mkdir -p "${log_dir}/logs/${shape}" \
        && cp -R framework/var/logs/. "${log_dir}/logs/${shape}/"
  fi
}

#---------------------------------------------------------------------------
# Main

printf 'Logs and results: %s/\n' "${log_dir}"

summary=""
failed=0
for shape in ${shapes}; do
  log="${log_dir}/${shape}.log"
  steps="${log_dir}/results/${shape}/smoke.tsv"
  rm -rf "${log_dir}/results/${shape}" "${log_dir}/logs/${shape}"
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
