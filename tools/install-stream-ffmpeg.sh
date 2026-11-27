#!/bin/bash
# Lynx-Tx stream out: its own ffmpeg (8.1, a static arm64 build from
# BtbN/FFmpeg-Builds on GitHub), for H.265 over Enhanced RTMP.
# Debian Bookworm's ffmpeg 5.1 cannot do it. The system ffmpeg is left
# alone: the transmitter's pipeline keeps using it.
#   sudo bash tools/install-stream-ffmpeg.sh
set -e
DEST=/usr/local/lib/lynx-tx/ffmpeg-stream
URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linuxarm64-gpl-8.1.tar.xz
[ "$(uname -m)" = aarch64 ] || { echo "This is for the ROCK (arm64)."; exit 1; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading ffmpeg 8.1 for arm64..."
curl -fL --progress-bar -o "$TMP/ff.tar.xz" "$URL"
tar -xJf "$TMP/ff.tar.xz" -C "$TMP"
mkdir -p "$DEST"
install -m 755 "$TMP"/ffmpeg-*/bin/ffmpeg "$DEST/ffmpeg"
"$DEST/ffmpeg" -hide_banner -version | head -1
echo "Installed in $DEST. Stream out picks it up within a few seconds; no restart needed."
