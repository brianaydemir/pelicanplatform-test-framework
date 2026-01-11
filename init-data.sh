#!/bin/bash

set -eux

rm -rf data/origin data/ads/input
mkdir -p data/origin data/ads/input

set +x


#---------------------------------------------------------------------------
# The intent here is that origin N exports `data/origin/N`. The naming
# scheme for the objects is intended to:
#
#   - allow the origins to export the same namespace but different objects
#   - avoid a cardinality explosion in Prometheus metrics

echo "Creating objects..."

for x in {0..0}; do
  mkdir -p "data/origin/$x/data"
  for y in {0..4095}; do echo "$x.$y.$RANDOM" > "data/origin/$x/data/$x.$y"; done
  echo "Created $(((x + 1) * 4096))"
done

for x in {1..7}; do ln -s "0" "data/origin/$x"; done


#---------------------------------------------------------------------------
# Create file transfer plugin ads given the above objects.

echo "Creating transfer ads..."


# Scenario: The namespace is public, and all objects exist.
for x in {0..2047}; do y="$RANDOM"; for z in {0..3}; do
  echo "[ Url=\"pelican://director:8444/public/data/0.$((RANDOM / 32))\"; LocalFileName=\"/dev/null\" ]" >>"data/ads/input/$x.$y.exist"
done; done


# Scenario: The namespace requires authorization, and all objects exist.
for x in {0..2047}; do y="$RANDOM"; for z in {0..1}; do
  echo "[ Url=\"pelican://director:8444/private/data/0.$((RANDOM / 64))\"; LocalFileName=\"/dev/null\" ]" >>"data/ads/input/$x.$y.token"
done; done


# Scenario: The namespace is public, but no objects exist.
for x in {0..2047}; do y="$RANDOM"; for z in {0..0}; do
  echo "[ Url=\"pelican://director:8444/public/data/9.$((RANDOM / 128))\"; LocalFileName=\"/dev/null\" ]" >>"data/ads/input/$x.$y.dne"
done; done


#---------------------------------------------------------------------------
# The end.

echo "Done."
