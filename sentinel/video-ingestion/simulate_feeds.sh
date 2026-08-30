#!/usr/bin/env bash
# Spin up N looping RTSP feeds from sample video files.
#
# Why this exists: do not bet a competition demo on 50 live cameras being
# reachable on the day. Venue Wi-Fi fails, a department's VPN drops, a DVR
# reboots. Run most of the 50 as looped republishes of pre-recorded footage
# and keep 10-15 genuinely live. This is standard practice for testing this
# class of system, it is reproducible, and you should say so openly -- a
# deterministic test harness reads as competence, not as a shortcut.
#
# Usage:  ./simulate_feeds.sh 35 ./data/samples rtsp://localhost:8554
set -euo pipefail

COUNT="${1:-35}"
SAMPLE_DIR="${2:-./data/samples}"
RTSP_BASE="${3:-rtsp://localhost:8554}"
PIDFILE="/tmp/sentinel-feeds.pids"

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg not found" >&2; exit 1
fi

mapfile -t SAMPLES < <(find "$SAMPLE_DIR" -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.ts' \) | sort)
if [ "${#SAMPLES[@]}" -eq 0 ]; then
  echo "No sample videos in $SAMPLE_DIR" >&2
  echo "Drop a few minutes of traffic footage there first." >&2
  exit 1
fi
echo "${#SAMPLES[@]} sample file(s); starting $COUNT feeds -> $RTSP_BASE"

: > "$PIDFILE"
for i in $(seq 1 "$COUNT"); do
  SRC="${SAMPLES[$(( (i - 1) % ${#SAMPLES[@]} ))]}"
  NAME=$(printf "sim-cam-%03d" "$i")

  # -re          : pace at real time, not as fast as the CPU allows
  # -stream_loop : loop forever, so the feed never ends mid-demo
  # -g 25        : 1-second GOP. The camera-side default of 50-100 adds up
  #                to 4s of startup delay and makes seek-to-evidence awful.
  # zerolatency  : no lookahead buffering
  # Each feed is offset by a random seek so 35 "cameras" are not all showing
  # the same frame of the same clip -- that looks obviously fake on stage
  # and, more importantly, would make cross-camera matching meaningless.
  OFFSET=$(( RANDOM % 60 ))

  ffmpeg -nostdin -loglevel error \
    -re -stream_loop -1 -ss "$OFFSET" -i "$SRC" \
    -an \
    -c:v libx264 -preset ultrafast -tune zerolatency \
    -g 25 -x264-params "keyint=25:min-keyint=25:scenecut=0" \
    -b:v 800k -maxrate 800k -bufsize 400k \
    -s 1280x720 -r 15 \
    -f rtsp -rtsp_transport tcp "$RTSP_BASE/$NAME" \
    >/dev/null 2>&1 &

  echo $! >> "$PIDFILE"
  printf "  %s <- %s (offset %ss)\n" "$NAME" "$(basename "$SRC")" "$OFFSET"
  sleep 0.25   # stagger; 35 simultaneous ffmpeg starts will thrash the disk
done

echo
echo "$COUNT feeds running. PIDs in $PIDFILE"
echo "Stop with: xargs kill < $PIDFILE"
