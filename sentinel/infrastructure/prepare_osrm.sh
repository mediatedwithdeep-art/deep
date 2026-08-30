#!/usr/bin/env bash
# Build the OSRM routing graph for Gujarat.
#
# This produces the travel-time matrix behind the spatio-temporal gate
# (docs/03 §4.3). Run once; takes ~10-25 min and ~6 GB RAM for Gujarat.
set -euo pipefail

DATA_DIR="${1:-./data/osrm}"
EXTRACT_URL="${2:-https://download.geofabrik.de/asia/india/gujarat-latest.osm.pbf}"
PBF="gujarat-latest.osm.pbf"

mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/$PBF" ]; then
  echo "Downloading Gujarat OSM extract (~120 MB)..."
  curl -fL --retry 4 --retry-delay 2 -o "$DATA_DIR/$PBF" "$EXTRACT_URL"
fi

# MLD (multi-level Dijkstra) rather than CH: MLD supports the large /table
# queries build_adjacency.py issues, and rebuilds far faster when the graph
# changes. CH is faster per query but /table degrades badly at scale.
run() { docker run --rm -v "$(realpath "$DATA_DIR"):/data" osrm/osrm-backend:v5.27.1 "$@"; }

echo "==> extract"; run osrm-extract -p /opt/car.lua "/data/$PBF"
echo "==> partition"; run osrm-partition "/data/gujarat-latest.osrm"
echo "==> customize"; run osrm-customize "/data/gujarat-latest.osrm"

echo
echo "Done. Start it with:  docker compose up -d osrm"
echo "Then:                 python3 scripts/build_adjacency.py --max-dist 5000"
