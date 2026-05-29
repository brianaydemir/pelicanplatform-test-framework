#!/bin/sh

set -eu
if [ $# -le 0 ]; then echo "Usage: $0 [up|down|restart|...]"; exit 1; fi
set -x

cmd="$1"

case "${cmd}" in

  up)
    set -- "$@" --detach --remove-orphans
    ;;

  restart)
    "$0" down && exec "$0" up
    ;;

  dev)
    "$0" up dev && exec "$0" exec -it dev bash -il
    ;;

esac

docker compose --env-file environment.cfg "$@"
