#!/bin/bash
#
# Create the test objects and transfer ads for the current shape.
#
#   ./init-data.sh                build them
#   ./init-data.sh --print-shape  print the shape and stop
#
# Normally run by `./fed.sh init`, which sets ORIGIN_VARIANT and
# COMPOSE_PROFILES from the presets. The shape built is recorded in
# data/.init-shape, which `./fed.sh up` checks.

set -eu

cd -- "$(dirname -- "$0")" || exit 1
progname=$(basename -- "$0")

die()       { printf '%s: %s\n' "${progname}" "$*" >&2; exit 1; }
die_usage() { printf '%s: %s\n' "${progname}" "$*" >&2; exit 2; }

usage() {
  sed -e '1d' -e '/^$/,$d' -e 's/^# \{0,1\}//' "./${progname}"
}

origin_variant=${ORIGIN_VARIANT:-posixv2}
profiles=${COMPOSE_PROFILES:-}

has_profile() {
  case ",${profiles}," in
    *",$1,"*) return 0 ;;
  esac
  return 1
}

if [ -f generated/federation-url ]; then
  federation=$(cat generated/federation-url)
else
  federation=pelican://discovery:8444
fi

# Knobs. Set them in the environment or a preset.
objects_per_origin=${OBJECTS_PER_ORIGIN:-512}
ads_per_scenario=${ADS_PER_SCENARIO:-2048}
urls_per_exist_ad=${URLS_PER_EXIST_AD:-4}
urls_per_token_ad=${URLS_PER_TOKEN_AD:-2}
urls_per_dne_ad=${URLS_PER_DNE_AD:-1}

# Which object an ad asks for: uniform over the corpus. Change this to
# skew the request distribution. It sets a variable rather than printing
# because $RANDOM in a subshell is not random on bash 3.2.
pick_object() { object=$(( RANDOM % objects_per_origin )); }

# Profiles are sorted so that `-p ha -p multi` equals `-p multi -p ha`.
print_shape() {
  local sorted
  sorted=$(printf '%s\n' "${profiles}" \
      | tr ',' '\n' | sed '/^$/d' | sort | tr '\n' ',' | sed 's/,$//')
  printf 'origin=%s profiles=%s federation=%s objects=%s\n' \
      "${origin_variant}" "${sorted:-none}" "${federation}" \
      "${objects_per_origin}"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --print-shape) print_shape; exit 0 ;;
  "") ;;
  *) die_usage "takes only --print-shape" ;;
esac

#---------------------------------------------------------------------------
# Where objects go. A pstore origin cannot be primed from the host.

prime_origin_0=no
prime_origin_1=no
prime_lab=no

case "${origin_variant}" in
  pstore) skip_reason="a 'pstore' origin cannot be primed from the host" ;;
  ssh)    skip_reason="an 'ssh' origin reads the lab server"
          prime_lab=yes ;;
  *)      prime_origin_0=yes ;;
esac

if [ "${prime_origin_0}" = yes ] && has_profile multi; then
  prime_origin_1=yes
fi
if has_profile lab; then
  prime_lab=yes
fi

#---------------------------------------------------------------------------
# Objects, named <origin>.<n>. Only the data/ subdirectory is replaced, so
# a pstore catalog beside it survives.

make_objects() {  # $1 = directory, $2 = name prefix
  local n
  rm -rf -- "$1" 2>/dev/null \
      || die "cannot remove $1, which holds files a container created;" \
             "remove it with 'sudo rm -rf -- $1', or start over with ./reset.sh"
  # Writable and readable by any uid (e.g. `alice` on the lab server).
  mkdir -p -- "$(dirname -- "$1")"
  mkdir -m 0777 -- "$1"
  for ((n = 0; n < objects_per_origin; n++)); do
    printf '%s.%s.%s\n' "$2" "${n}" "${RANDOM}" >"$1/$2.$n"
  done
  # No `--`: macOS chmod does not accept it.
  chmod -R a+rX "$1"
}

if [ "${prime_origin_0}" = yes ]; then
  printf 'Creating %s objects for origin-0 ...\n' "${objects_per_origin}"
  make_objects data/origin/0/data 0
else
  printf 'Skipping origin-0: %s.\n' "${skip_reason}"
fi

if [ "${prime_origin_1}" = yes ]; then
  printf 'Creating %s objects for origin-1 ...\n' "${objects_per_origin}"
  make_objects data/origin/1/data 1
  # The director may send any request to either origin, so origin-1 also
  # gets origin-0's objects.
  cp -R data/origin/0/data/. data/origin/1/data/
elif has_profile multi; then
  printf 'Skipping origin-1: %s.\n' "${skip_reason}"
fi

if [ "${prime_lab}" = yes ]; then
  # Named like origin-0's, so the same ads work against either.
  printf 'Creating %s objects for the lab server ...\n' "${objects_per_origin}"
  make_objects data/lab-server/data 0
fi

#---------------------------------------------------------------------------
# Transfer ads: HTCondor file-transfer plugin input for stash_plugin.
#
#   exist  public/data/0.<n>   objects that exist
#   token  private/data/0.<n>  objects that exist, behind a token
#   dne    public/data/9.<n>   objects that do not exist
#
# Files are named <index>.<random>.<scenario> so that run.sh, which walks
# them in order, interleaves the scenarios.

printf 'Creating transfer ads ...\n'

rm -rf data/ads/input
mkdir -p data/ads/input

make_ads() {  # $1 = scenario, $2 = URLs per ad, $3 = object name prefix
  local x y z object lines
  for ((x = 0; x < ads_per_scenario; x++)); do
    y=$RANDOM
    lines=""
    for ((z = 0; z < $2; z++)); do
      pick_object
      lines+="[ Url=\"${federation}/$3.${object}\"; LocalFileName=\"/dev/null\" ]"$'\n'
    done
    printf '%s' "${lines}" >"data/ads/input/$x.$y.$1"
  done
}

make_ads exist "${urls_per_exist_ad}" public/data/0
make_ads token "${urls_per_token_ad}" private/data/0
make_ads dne "${urls_per_dne_ad}" public/data/9

print_shape >data/.init-shape

printf 'Done: %s\n' "$(cat data/.init-shape)"
