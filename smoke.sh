#!/bin/sh
#
# Run Pelican's smoke tests over a matrix of shapes: for each shape, start
# from scratch, start the federation, run the tests (./fed.sh test, which
# runs every suite that applies), and stop. It all happens in copies of
# the checkout, which it leaves alone.
#
#   ./smoke.sh              every shape
#   ./smoke.sh default ssh-v2
#                           only these shapes
#   ./smoke.sh -p origin-pstore -p topo-multi-cache
#                           the shape of these presets (and any named)
#   ./smoke.sh -j 3 ...     run three copies at once
#   ./smoke.sh -o DIR ...   put logs and results in DIR
#   ./smoke.sh -l           list the shapes and what each runs
#   ./smoke.sh --names      list only the shapes' names, one per line
#   ./smoke.sh --table      list them for framework/matrix.py
#
# Given no shapes and no -p, it runs those in framework/smoke-shapes.tsv,
# which framework/matrix.py generates: two baselines, default (the
# framework's own choices) and pelican-defaults (Pelican's), and as many
# other shapes, each as simple as it can be, as it takes to make every
# useful pair of choices that the presets offer (see "The smoke matrix"
# in docs/development.md). Some shapes also rotate every key, restart,
# and run every suite again. The shape of -p's presets, named
# +<preset>+<preset>..., does not.
#
# It runs in N copies of the checkout (-j N; 1 by default), made in
# DIR/.work/: the files that Git does not ignore, local/, and
# framework/var/bin/, so that your changes, settings, images, and client
# are what get tested. No copy mounts a Pelican checkout at /app in the
# dev container, whatever PELICAN_SRC_DIR says: the tests use the
# binaries in framework/var/bin/. Copy n is a Compose project of its own,
# pelican-<n>, with its ports 100*n above those that README.md names
# (PORT_OFFSET), so that the copies, and a federation in the checkout,
# run side by side. Two runs of smoke.sh at once would clash. Each copy
# takes the next shape in turn, and starts it from scratch. With fewer
# shapes than copies, each shape's suites are shared out among
# N/<shapes> copies, rounded up (see --part in test.py), each running
# the shape, and their results are put together as one run's. At the
# end, each copy is reset and removed.
#
# Logs and results go to DIR (default: $SMOKE_LOG_DIR, or
# smoke-logs/<timestamp>): <shape>.log, results/<shape>/*.tsv, the
# services' logs from each run of the tests, logs/<shape>/<time>/ (with
# -part<i> after <time> for a shape shared out), and, merged over every
# shape, results.tsv, junit.xml, and summary.md. A shape that fails also
# leaves <shape>.compose.log, every service's log when it failed. Exits
# non-zero if any shape fails. On SIGINT or SIGTERM, it stops the copies
# at once, and puts what they had done in DIR, failing each shape, or
# part of one, that they had not finished.

set -u

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")
top=$(pwd -P)

# Never a Pelican checkout at /app in the copies' dev containers (see
# make_copy, which also sets it empty in each copy's local/environment.cfg,
# since fed.sh takes the caller's value only when it is not empty).
unset PELICAN_SRC_DIR

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }
warn()      { printf '%s: warning: %s\n' "${progname}" "$*" >&2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

# The shapes: each line of framework/smoke-shapes.tsv but its comments,
# with a shape's name, whether it rotates every key (yes or no), and its
# presets, separated by tabs.
shapes_file=framework/smoke-shapes.tsv
[ -r "${shapes_file}" ] || die "cannot read ${shapes_file}"
shape_rows() { grep -v -e '^#' -e '^[[:space:]]*$' "${shapes_file}"; }
all_shapes=$(shape_rows | cut -f 1 | tr '\n' ' ')

# The shape of -p's presets, if any, and its presets.
custom_shape=""
custom_presets=""

# Each shape's presets, one per word; fails if there is no such shape.
shape_presets() {
  if [ -n "${custom_shape}" ] && [ "$1" = "${custom_shape}" ]; then
    printf '%s\n' "${custom_presets}"
    return 0
  fi
  shape_rows | awk -F '\t' -v shape="$1" \
      '$1 == shape { print $3; found = 1 } END { exit !found }'
}

# Whether the shape rotates every key after the tests, then restarts and
# runs every suite again.
shape_rotates() {
  shape_rows | awk -F '\t' -v shape="$1" \
      '$1 == shape && $2 == "yes" { found = 1 } END { exit !found }'
}

# What the shape does after the tests, if anything, for --list.
shape_notes() {
  if shape_rotates "$1"; then
    printf 'then rotates every key, restarts, and reruns every suite\n'
  fi
}

# The number of words in $@.
count() { printf '%s\n' "$#"; }

log_dir=${SMOKE_LOG_DIR:-}
jobs=1
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
    -j) [ $# -ge 2 ] || die_usage "-j needs a number of copies"
        jobs=$2; shift 2 ;;
    -j*) jobs=${1#-j}; shift ;;
    -o) [ $# -ge 2 ] || die_usage "-o needs a directory"
        log_dir=$2; shift 2 ;;
    -o*) log_dir=${1#-o}; shift ;;
    -p) [ $# -ge 2 ] || die_usage "-p needs a preset name"
        custom_presets="${custom_presets}${custom_presets:+ }$2"; shift 2 ;;
    -p*) custom_presets="${custom_presets}${custom_presets:+ }${1#-p}"; shift ;;
    --) shift; break ;;
    -*) die_usage "unknown option: $1" ;;
    *) break ;;
  esac
done

case "${jobs}" in
  ''|0*|*[!0-9]*) die_usage "-j takes a number of copies, from 1" ;;
esac

# The shapes to run, each once: those named, or every one, and then
# -p's.
if [ -n "${custom_presets}" ]; then
  custom_shape=+$(printf '%s' "${custom_presets}" | tr ' ' '+')
fi
named=${*:-}
if [ -z "${named}" ] && [ -z "${custom_shape}" ]; then
  named=${all_shapes}
fi
shapes=""
for shape in ${named} ${custom_shape}; do
  shape_presets "${shape}" >/dev/null \
      || die_usage "no such shape: ${shape} (see ./${progname} --names)"
  case " ${shapes} " in
    *" ${shape} "*) ;;
    *) shapes="${shapes}${shapes:+ }${shape}" ;;
  esac
done
[ -n "${shapes}" ] || die "no shapes to run"

for cmd in docker curl uv git tar pgrep; do
  command -v "${cmd}" >/dev/null 2>&1 || die "${cmd} is not in PATH"
done
docker info >/dev/null 2>&1 || die "cannot reach the Docker daemon"

if [ -n "${custom_shape}" ]; then
  set --
  for preset in ${custom_presets}; do
    set -- "$@" -p "${preset}"
  done
  ./fed.sh "$@" check || exit 2
fi

log_dir=${log_dir:-smoke-logs/$(date +%Y%m%d-%H%M%S)}
shown_dir=${log_dir}
mkdir -p "${log_dir}/results" || exit 1
# Absolute, for the copies.
log_dir=$(cd -- "${log_dir}" && pwd -P) || exit 1
work="${log_dir}/.work"
[ ! -e "${work}" ] \
    || die "${shown_dir}/.work is from a run that was killed; run ./reset.sh" \
           "in each copy-<n>/ there, then remove it"

# Each shape is shared out among this many copies, as parts 1 to ${parts}
# of its suites. The items to run are each shape's parts, as
# <shape>.<part>.
# shellcheck disable=SC2086  # one shape per word
parts=$(( (jobs + $(count ${shapes}) - 1) / $(count ${shapes}) ))
items=""
for shape in ${shapes}; do
  part=1
  while [ "${part}" -le "${parts}" ]; do
    items="${items}${items:+ }${shape}.${part}"
    part=$(( part + 1 ))
  done
done

#---------------------------------------------------------------------------
# The copies, ${work}/copy-<n>/

# The files that a copy holds: each that Git does not ignore (but none in
# DIR, if the checkout holds it), local/, and framework/var/bin/.
copy_list() {
  inside=""
  case "${log_dir}/" in
    "${top}/"*) inside=${log_dir#"${top}/"}/ ;;
  esac
  git ls-files -z --cached --others --exclude-standard | tr '\0' '\n' \
    | while IFS= read -r path; do
        # Deleted, but still in the index.
        [ -e "${path}" ] || [ -L "${path}" ] || continue
        if [ -n "${inside}" ]; then
          case "${path}" in
            "${inside}"*) continue ;;
          esac
        fi
        printf '%s\n' "${path}"
      done
  for path in local framework/var/bin; do
    [ ! -e "${path}" ] || printf '%s\n' "${path}"
  done
}

# Make copy $1, a Compose project of its own, with ports of its own (see
# fed.sh).
make_copy() {
  copy="${work}/copy-$1"
  mkdir -p "${copy}" || return 1
  copy_list | tar -cf - -T - | (cd -- "${copy}" && tar -xf -)
  [ -x "${copy}/fed.sh" ] && mkdir -p "${copy}/local" || return 1
  printf '\n# Added by smoke.sh, for copy %s of the checkout.\n' "$1" \
      >>"${copy}/local/environment.cfg"
  printf 'COMPOSE_PROJECT_NAME=pelican-%s\nPORT_OFFSET=%s\nPELICAN_SRC_DIR=\n' \
      "$1" "$(( $1 * 100 ))" >>"${copy}/local/environment.cfg"
}

# Stop copy $1's federation, discard what it made, and remove the copy.
# Leaves it, and says so, if that fails.
discard_copy() {
  copy="${work}/copy-$1"
  [ -d "${copy}" ] || return 0
  if (cd -- "${copy}" && ./reset.sh) >"${copy}.reset.log" 2>&1 && rm -rf "${copy}"; then
    rm -f "${copy}.reset.log"
    return 0
  fi
  warn "could not reset and remove ${copy}/; see ${copy}.reset.log"
  return 1
}

# Discard every copy, and then, unless one is left, ${work}.
cleanup() {
  kept=""
  n=1
  while [ "${n}" -le "${jobs}" ]; do
    discard_copy "${n}" || kept=yes
    n=$(( n + 1 ))
  done
  if [ -n "${kept}" ]; then
    warn "kept ${work}/"
  else
    rm -rf "${work}"
  fi
}

# Process $1's descendants, one per line.
descendants() {
  for child in $(pgrep -P "$1"); do
    printf '%s\n' "${child}"
    descendants "${child}"
  done
}

# On an interrupt: kill each worker, and everything it runs, then finish
# with what the parts left, and exit with status $1. They are killed
# outright, since whatever they would put back is in the copies: a part
# cut short leaves only its log and its steps so far.
workers=""
stop() {
  trap '' INT TERM
  printf '\n%s: stopping, putting together what has run, and discarding the copies ...\n' \
      "${progname}" >&2
  if [ -n "${workers}" ]; then
    pids=""
    for pid in ${workers}; do
      pids="${pids} ${pid} $(descendants "${pid}" | tr '\n' ' ')"
    done
    # shellcheck disable=SC2086  # one PID per word
    kill -KILL ${pids} 2>/dev/null
    # Quietly: some shells report each job that was killed.
    wait 2>/dev/null
  fi
  finish
  exit "$1"
}

#---------------------------------------------------------------------------
# Running a part of a shape, in a copy

# Record a row of the shape's own results (see framework/testlib/report.py):
# step $1, status $2, and seconds $3.
record() {
  if [ ! -f "${steps}" ]; then
    printf 'suite\tcase\tstatus\tseconds\tnote\n' >"${steps}"
  fi
  printf 'smoke\t%s\t%s\t%s\t\n' "$1" "$2" "$3" >>"${steps}"
}

# Run step $1, the command $2..., appending its output to ${log}.
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

# Run ./reset.sh, keeping framework/var/bin/ in .bin meanwhile.
fresh_start() {
  if [ -e .bin ]; then
    printf '.bin is in the way of keeping framework/var/bin/\n'
    return 1
  fi
  if [ -d framework/var/bin ]; then
    mv framework/var/bin .bin || return 1
  fi
  ./reset.sh
  rc=$?
  if [ -d .bin ]; then
    mkdir -p framework/var && mv .bin framework/var/bin || return 1
  fi
  return "${rc}"
}

up()      { ./fed.sh up --wait --wait-timeout 300; }
restart() { ./fed.sh restart --wait --wait-timeout 300; }

# The tests of this part, in the dev container (see test.py). A rerun's
# suites are recorded as <suite>-<tag> (see RESULTS_TAG in
# framework/testlib/report.py), beside the first run's.
tests()         { ./fed.sh test --part "${part}/${parts}"; }
tests_rotated() { RESULTS_TAG=rotated-keys ./fed.sh test --part "${part}/${parts}"; }

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

# What the shape runs after `up`: the tests run every suite of this part
# that applies to the shape. A shape that rotates every key then
# restarts, so that every server rereads its keys, and runs them again.
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

# Keep the part's results (framework/var/results/, which the next reset
# discards) beside its steps', and the services' logs from each run of
# the tests (framework/var/logs/<time>/; see fed.sh's fed_save_logs).
save_results() {
  for tsv in framework/var/results/*.tsv; do
    # Regular files alone. The dev container writes framework/var/results/,
    # so put-file.py copies each: it follows no symlink, not even one put
    # in the file's place after this test.
    [ -f "${tsv}" ] && [ ! -L "${tsv}" ] || continue
    framework/put-file.py "${tsv}" "${out}/results/${shape}/"
  done
  if [ -d framework/var/logs ]; then
    mkdir -p "${out}/logs/${shape}" \
        && cp -R framework/var/logs/. "${out}/logs/${shape}/"
  fi
}

# Run part $2 of shape $1 here, in a copy, from scratch, with its logs
# and results in ${work}/<shape>.<part>/, laid out as in DIR. Its status
# and seconds go to `result` there.
run_part() {
  shape=$1
  part=$2
  out="${work}/${shape}.${part}"
  log="${out}/${shape}.log"
  steps="${out}/results/${shape}/smoke.tsv"
  mkdir -p "${out}/results/${shape}" || return 1
  start=$(date +%s)
  set --
  for preset in $(shape_presets "${shape}"); do
    set -- "$@" -p "${preset}"
  done

  result=PASS
  fresh=""
  if ! { step "reset" fresh_start \
           && fresh=yes \
           && step "init" ./fed.sh "$@" init \
           && step "status" ./fed.sh status \
           && step "up" up \
           && shape_checks "${shape}"; }; then
    result=FAIL
    ./fed.sh ps --all >"${out}/${shape}.compose.log" 2>&1
    # With each token's signature cut out, as fed_redact_tokens in fed.sh
    # does.
    ./fed.sh logs --no-color --timestamps 2>&1 \
        | sed -E 's/(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{8,}/\1.<signature>/g' \
        >>"${out}/${shape}.compose.log"
  fi
  # After a failed reset, framework/var/results/ may be another shape's.
  if [ -n "${fresh}" ]; then
    save_results
  fi
  step "down" ./fed.sh down || true
  printf '%s %s\n' "${result}" "$(( $(date +%s) - start ))" >"${out}/result"
}

# Name item $1 (<shape>.<part>) as the output does.
title() {
  if [ "${parts}" -eq 1 ]; then
    printf '%s\n' "${1%.*}"
  else
    printf '%s (part %s of %s)\n' "${1%.*}" "${1##*.}" "${parts}"
  fi
}

# Run item $1 in copy $2, and then print what it printed, at once, so
# that the copies' output does not interleave.
run_item() {
  mkdir -p "${work}/$1" || return 1
  printf '%s: started in copy %s\n' "$(title "$1")" "$2"
  {
    printf '\n%s (%s)\n' "$(title "$1")" "$(shape_presets "${1%.*}")"
    run_part "${1%.*}" "${1##*.}"
  } >"${work}/$1/console" 2>&1
  cat "${work}/$1/console"
}

# In copy $1, run each item that no other copy has claimed, in turn.
worker() {
  cd -- "${work}/copy-$1" || exit 1
  for item in ${items}; do
    mkdir "${work}/claims/${item}" 2>/dev/null || continue
    run_item "${item}" "$1"
  done
}

#---------------------------------------------------------------------------
# Putting the parts together

# Put shape $1's parts together in DIR, as one run's, and add its line
# to ${summary}.
assemble() {
  shape=$1
  rm -rf "${log_dir}/results/${shape}" "${log_dir}/logs/${shape}"
  rm -f "${log_dir}/${shape}.log" "${log_dir}/${shape}.compose.log"
  result=PASS
  took=0
  set --
  part=1
  while [ "${part}" -le "${parts}" ]; do
    out="${work}/${shape}.${part}"
    if [ -f "${out}/result" ] && read -r status seconds <"${out}/result"; then
      [ "${status}" = PASS ] || result=FAIL
      [ "${seconds}" -le "${took}" ] || took=${seconds}
    else
      # Cut short, or never run: say so, as a suite that stops early does
      # (see framework/testlib/report.py).
      result=FAIL
      steps="${out}/results/${shape}/smoke.tsv"
      mkdir -p "${out}/results/${shape}" && record aborted FAIL ""
    fi
    [ ! -d "${out}/results/${shape}" ] || set -- "$@" "${out}/results/${shape}"
    for file in "${shape}.log" "${shape}.compose.log"; do
      [ -f "${out}/${file}" ] || continue
      {
        [ "${parts}" -eq 1 ] || printf '\n==> part %s of %s\n' "${part}" "${parts}"
        cat "${out}/${file}"
      } >>"${log_dir}/${file}"
    done
    for run in "${out}/logs/${shape}"/*/; do
      [ -d "${run}" ] || continue
      run=${run%/}
      name=${run##*/}
      [ "${parts}" -eq 1 ] || name="${name}-part${part}"
      mkdir -p "${log_dir}/logs/${shape}" && mv "${run}" "${log_dir}/logs/${shape}/${name}"
    done
    part=$(( part + 1 ))
  done
  if [ $# -gt 0 ]; then
    framework/report.py combine "${log_dir}/results/${shape}" "$@" || result=FAIL
  fi
  [ "${result}" = PASS ] || failed=$(( failed + 1 ))
  summary="${summary}$(printf '%-26s %s (%ss)' "${shape}" "${result}" "${took}")
"
}

# Put each shape's parts together in DIR, and merge every shape's
# results there, then discard the copies, and print each shape's result.
# Fails if any shape failed, or if the merged results could not be
# written.
finish() {
  summary=""
  failed=0
  for shape in ${shapes}; do
    assemble "${shape}"
  done

  # One table over every shape, and its conversions for CI. Results that
  # an earlier run left in ${log_dir} for other shapes are merged too.
  report_ok=yes
  framework/report.py merge "${log_dir}/results"/*/ >"${log_dir}/results.tsv" \
      && framework/report.py junit "${log_dir}/results.tsv" >"${log_dir}/junit.xml" \
      && framework/report.py markdown "${log_dir}/results.tsv" >"${log_dir}/summary.md" \
      || report_ok=""

  cleanup

  printf '\n%s' "${summary}"
  printf 'Logs and results: %s/\n' "${shown_dir}"
  if [ -z "${report_ok}" ]; then
    printf '%s: could not write %s/results.tsv, junit.xml, or summary.md\n' \
        "${progname}" "${shown_dir}" >&2
    return 1
  fi
  [ "${failed}" -eq 0 ]
}

#---------------------------------------------------------------------------
# Main

printf 'Logs and results: %s/\n' "${shown_dir}"

trap 'stop 130' INT
trap 'stop 143' TERM

mkdir -p "${work}/claims" || exit 1
n=1
while [ "${n}" -le "${jobs}" ]; do
  make_copy "${n}" || { warn "could not copy the checkout to ${work}/copy-${n}/"; stop 1; }
  n=$(( n + 1 ))
done

n=1
while [ "${n}" -le "${jobs}" ]; do
  worker "${n}" &
  workers="${workers} $!"
  n=$(( n + 1 ))
done
wait
workers=""

# Not to be interrupted, since stop() would put the parts together again,
# without the services' logs that assemble has already moved.
trap '' INT TERM
finish
