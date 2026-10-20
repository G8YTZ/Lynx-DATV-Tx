#!/bin/bash
# ============================================================
#  Lynx-DATV-Tx installer
#
#  One script, everything: packages (GNU Radio with Ron Economos W6RZ's
#  DVB-S2 blocks), TSDuck, the video loopback, Ron's gr-atsc3, the patched Rockchip HEVC encoder,
#  Lynx-Tx itself, the direct link to the SDR and the service that
#  starts it at boot.
#
#  For a Radxa ROCK 5T on Radxa's Debian 12 image, run as the normal
#  user (radxa), not root:
#
#    curl -sSL https://raw.githubusercontent.com/G8YTZ/Lynx-DATV-Tx/main/install.sh | bash
#
#  Safe to run again: each step checks whether it is already done, so a
#  second run only updates the code and fills in anything missing.
#  About 15 minutes on a fresh ROCK (timed 20 Oct 2026), most of it compiling.
# ============================================================
set -euo pipefail

REPO_URL="https://github.com/G8YTZ/Lynx-DATV-Tx.git"
BRANCH="main"
if [ "${1:-}" = "--branch" ] && [ -n "${2:-}" ]; then BRANCH="$2"; shift 2; fi

APP_DIR="$HOME/lynx-tx"
SRC_DIR="$HOME/src"
MEDIA_DIR="/srv/lynxdeck/media"
SDR_PORT="${LYNX_SDR_PORT:-enP4p65s0}"     # the ROCK 5T's second RJ45
ARCH_LIB="/usr/lib/aarch64-linux-gnu"
PLUGIN="$ARCH_LIB/gstreamer-1.0/libgstrockchipmpp.so"

step() { echo; echo "=== $* ==="; }
note() { echo ">>> $*"; }
warn() { echo "!!! $*"; WARNINGS+=("$*"); }
WARNINGS=()

echo "=== Lynx-DATV-Tx installer (branch: $BRANCH) ==="

# --- Sanity ---------------------------------------------------
if [ "$(id -u)" = "0" ]; then
  echo "Run this as your normal user (radxa), not root: it uses sudo where it must."; exit 1
fi
if [ "$(uname -m)" != "aarch64" ]; then
  echo "This is for the ROCK 5T (aarch64); this machine is $(uname -m)."; exit 1
fi
. /etc/os-release
note "System: $PRETTY_NAME, kernel $(uname -r)"
sudo -v

# --- 1. Packages ----------------------------------------------
step "1/9 Packages"
sudo apt-get update
sudo apt-get install -y \
  git curl jq ca-certificates \
  python3 python3-fastapi python3-uvicorn python3-pydantic python3-yaml python3-numpy python3-gi \
  ffmpeg v4l-utils \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gir1.2-gstreamer-1.0 \
  gnuradio gnuradio-dev libiio-utils mosquitto-clients sshpass \
  cmake build-essential pkg-config pybind11-dev python3-pybind11 \
  libspdlog-dev libgmp-dev liborc-0.4-dev \
  network-manager dnsmasq-base

if ! gst-inspect-1.0 mpph265enc >/dev/null 2>&1; then
  echo "The Rockchip hardware encoder (mpph265enc) is missing."
  echo "Lynx-Tx needs Radxa's own Debian image for the ROCK 5T, which includes it."; exit 1
fi

# --- 2. Lynx-Tx ----------------------------------------------
step "2/9 Lynx-Tx"
if [ -d "$APP_DIR/.git" ] && [ "$(git -C "$APP_DIR" remote get-url origin)" != "$REPO_URL" ] &&
   [ "$(git -C "$APP_DIR" remote get-url origin).git" != "$REPO_URL" ]; then
  warn "$APP_DIR comes from $(git -C "$APP_DIR" remote get-url origin), not $REPO_URL: code left as it is."
elif [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" pull -q --ff-only origin "$BRANCH"
  note "Updated to $(git -C "$APP_DIR" log -1 --format='%h %s')"
else
  git clone -q -b "$BRANCH" "$REPO_URL" "$APP_DIR"
  note "Installed $(git -C "$APP_DIR" log -1 --format='%h %s')"
fi
mkdir -p "$APP_DIR/config"
if [ ! -f "$APP_DIR/config/lynx_tx.yaml" ]; then
  cp "$APP_DIR/lynx_tx.example.yaml" "$APP_DIR/config/lynx_tx.yaml"
  note "Config created: set your callsign on the page or in config/lynx_tx.yaml"
fi
sudo mkdir -p "$MEDIA_DIR" && sudo chown "$USER": "$MEDIA_DIR"

# --- 3. TSDuck ------------------------------------------------
step "3/9 TSDuck"
if command -v tsp >/dev/null && command -v tstabcomp >/dev/null; then
  note "Already installed: $(tsp --version 2>&1 | head -1)"
else
  # TSDuck stopped building for Debian 12 after 3.39 (newer releases are
  # Debian 13 and Ubuntu only), so take the newest release that still has a
  # debian${VERSION_ID} arm64 package, else the last known one: 3.39-3956.
  url=$(curl -sSL "https://api.github.com/repos/tsduck/tsduck/releases?per_page=30" 2>/dev/null |
        jq -r --arg v "debian${VERSION_ID}_arm64.deb" \
        '[.[]?.assets[]? | select(.name | startswith("tsduck_") and endswith($v)) | .browser_download_url][0] // empty' \
        2>/dev/null || true)
  if [ -z "$url" ]; then
    url="https://github.com/tsduck/tsduck/releases/download/v3.39-3956/tsduck_3.39-3956.debian${VERSION_ID}_arm64.deb"
  fi
  tmp=$(mktemp -d); chmod 755 "$tmp"   # apt reads it as user _apt
  curl -fsSL -o "$tmp/tsduck.deb" "$url" ||
    { echo "Could not download TSDuck from $url"; echo "Install it by hand from github.com/tsduck/tsduck/releases, then re-run."; exit 1; }
  sudo apt-get install -y "$tmp/tsduck.deb"; rm -rf "$tmp"
  note "Installed $(basename "$url")"
fi

# --- 4. Video loopback (the media player's way into the encoder) ---
step "4/9 Video loopback /dev/video10"
if ! modinfo v4l2loopback >/dev/null 2>&1; then
  sudo apt-get install -y "linux-headers-$(uname -r)" v4l2loopback-dkms ||
    warn "Could not install v4l2loopback: the media player will not work until it is."
fi
# Deliberately without exclusive_caps: with it, ffmpeg's format probe fails
# on a device nothing has written to yet (see lynx_tx.py, LOOPBACK_DEVICE).
echo "v4l2loopback" | sudo tee /etc/modules-load.d/lynx-tx.conf >/dev/null
echo 'options v4l2loopback video_nr=10 max_width=3840 max_height=2160 card_label="Lynx Deck"' |
  sudo tee /etc/modprobe.d/lynx-tx-v4l2loopback.conf >/dev/null
if [ ! -e /dev/video10 ]; then
  sudo modprobe v4l2loopback 2>/dev/null || warn "v4l2loopback did not load; check after a restart."
fi
[ -e /dev/video10 ] && note "/dev/video10 ready"

# Contiguous memory for 4K: Radxa's image reserves 256 MB, shared by HDMI in,
# the decoder, the encoder and RGA. One 4K frame is 16.6 MB; after hours on
# air there was no such gap left, HDMI in failed its allocation and its error
# path corrupted the kernel (ROCK froze, 18 and 19 Oct 2026). 1 GB fixes it.
# /etc/kernel/cmdline is Radxa's master copy; extlinux.conf is what boots.
CMA_REBOOT=0
if [ -f /etc/kernel/cmdline ] && ! grep -q 'cma=' /etc/kernel/cmdline; then
  sudo sed -i '1s/$/ cma=1024M/' /etc/kernel/cmdline; CMA_REBOOT=1
fi
if [ -f /boot/extlinux/extlinux.conf ] && grep -qE '^[[:space:]]*append ' /boot/extlinux/extlinux.conf &&
   grep -E '^[[:space:]]*append ' /boot/extlinux/extlinux.conf | grep -qv 'cma='; then
  sudo cp -n /boot/extlinux/extlinux.conf /boot/extlinux/extlinux.conf.lynx-bak
  sudo sed -i '/^[[:space:]]*append /{/cma=/!s/$/ cma=1024M/}' /boot/extlinux/extlinux.conf
  CMA_REBOOT=1
fi
# HDMI in gets a fixed block of its own (no-map, below 4 GB), never lent to
# the page cache: tools/lynx-hdmirx-mem.dts. Radxa's u-boot-update lists
# every .dtbo in /boot/dtbo as an fdtoverlay.
# DISABLED 19 Oct 2026: on the dev ROCK this was followed within minutes by
# memory corruption (segfaults everywhere, panic on restart). Not to return
# until proven on a spare ROCK. Set LYNX_HDMIRX_OVERLAY=1 only for that test.
if [ "${LYNX_HDMIRX_OVERLAY:-0}" = 1 ] && [ -d /boot/dtbo ] && [ ! -f /boot/dtbo/lynx-hdmirx-mem.dtbo ]; then
  sudo apt-get install -y device-tree-compiler >/dev/null 2>&1 || true
  if sudo dtc -@ -q -I dts -O dtb -o /boot/dtbo/lynx-hdmirx-mem.dtbo "$APP_DIR/tools/lynx-hdmirx-mem.dts" &&
     command -v u-boot-update >/dev/null && sudo u-boot-update >/dev/null; then
    CMA_REBOOT=1; note "HDMI in: fixed 256 MB block, after a restart"
  else
    warn "Could not add the HDMI-in memory overlay: 4K HDMI in may fail after hours on air."
  fi
fi
# No swap: a live 4K chain must never be paged out, and 400 MB of real use
# in 8 GB never needs it. Radxa's image swaps to compressed RAM (zramswap).
if systemctl is-enabled zramswap.service >/dev/null 2>&1; then
  sudo systemctl disable --now zramswap.service >/dev/null 2>&1 && note "Swap off (zramswap)"
fi
if grep -q 'cma=' /proc/cmdline; then
  :
elif grep -qs 'cma=' /boot/extlinux/extlinux.conf; then
  # Set, by this run or an earlier one, but not active until a restart.
  CMA_REBOOT=1; note "Contiguous memory set to 1 GB: takes effect after a restart"
else
  warn "Could not set cma=1024M on the kernel command line: 4K HDMI in may fail after hours on air."
fi

# --- 5. gr-atsc3 (ATSC 3.0) ----------------------------------
step "5/9 gr-atsc3, Ron Economos W6RZ (ATSC 3.0 modulator)"
# Older gr-atsc3 installed itself as gnuradio.atsc3, current builds as plain
# atsc3 (its blocks then say "import atsc3"): either is fine.
atsc3_ok() { python3 -c "import atsc3" 2>/dev/null || python3 -c "from gnuradio import atsc3" 2>/dev/null; }
if atsc3_ok; then
  note "Already installed"
else
  mkdir -p "$SRC_DIR"
  [ -d "$SRC_DIR/gr-atsc3/.git" ] || git clone https://github.com/drmpeg/gr-atsc3 "$SRC_DIR/gr-atsc3"
  if (cd "$SRC_DIR/gr-atsc3" && mkdir -p build && cd build &&
      cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig) &&
     atsc3_ok; then
    note "Built and installed"
  else
    warn "gr-atsc3 did not build or import: ATSC 3.0 mode will not start until it does."
  fi
fi

# --- 6. Patched HEVC encoder (live QP floor for hybrid rate control) ---
step "6/9 Rockchip HEVC encoder, patched for live rate control"
if grep -q "rc:fqp_min_p" "$PLUGIN" 2>/dev/null; then
  note "Patched encoder already installed"
else
  if SRC_DIR="$SRC_DIR" APP_DIR="$APP_DIR" ARCH_LIB="$ARCH_LIB" PLUGIN="$PLUGIN" \
     bash -euo pipefail <<'BUILD'
    # plugins-base dev would drag in an older Mesa than Radxa's, so only
    # its headers are unpacked privately; nothing in the system changes.
    sudo apt-get install -y meson ninja-build libgstreamer1.0-dev librockchip-mpp-dev librga-dev liborc-0.4-dev
    GSTDEV="$SRC_DIR/gstdev"; rm -rf "$GSTDEV"; mkdir -p "$GSTDEV"; cd "$GSTDEV"
    apt-get download libgstreamer-plugins-base1.0-dev
    dpkg -x libgstreamer-plugins-base1.0-dev_*.deb .
    PC="$GSTDEV/usr/lib/aarch64-linux-gnu/pkgconfig"
    sed -i "s|^prefix=.*|prefix=$GSTDEV/usr|" "$PC"/*.pc
    for l in "$GSTDEV"/usr/lib/aarch64-linux-gnu/*.so; do
      [ -e "$l" ] || ln -sf "$ARCH_LIB/$(readlink "$l")" "$l"
    done
    rm -rf "$SRC_DIR/gst-rockchip-src"
    git clone -q --depth 1 https://github.com/radxa-pkg/gstreamer-rockchip.git "$SRC_DIR/gst-rockchip-src"
    cd "$SRC_DIR/gst-rockchip-src"
    git apply "$APP_DIR/patches/gst-rockchip-live-qp-floor.patch"
    PKG_CONFIG_PATH="$PC" meson setup build
    ninja -C build
    built=$(find build -name libgstrockchipmpp.so | head -1)
    [ -n "$built" ]
    # Keep Radxa's plugin as .stock, so a package upgrade cannot quietly
    # replace ours and it can always be put back.
    sudo dpkg-divert --local --rename --divert "$PLUGIN.stock" --add "$PLUGIN"
    sudo cp "$built" "$PLUGIN"
    rm -rf "$HOME/.cache/gstreamer-1.0"
BUILD
  then
    note "Patched encoder installed (Radxa's original kept as $PLUGIN.stock)"
  else
    warn "Patched encoder did not build: Lynx-Tx will run with plain CBR rate control."
  fi
fi

# --- 7. Restart and Shut down buttons --------------------------
step "7/9 Restart and Shut down buttons"
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff" |
  sudo tee /etc/sudoers.d/lynx-tx >/dev/null
sudo chmod 440 /etc/sudoers.d/lynx-tx
sudo visudo -c -f /etc/sudoers.d/lynx-tx >/dev/null && note "sudoers rule in place"

# --- 8. Direct link to the SDR ---------------------------------
step "8/9 Direct link to the SDR ($SDR_PORT)"
# The ROCK's SSH key: Lynx-Tx puts it on a PlutoSky by itself the first time
# it sees one (tezuka_prep.py), then never needs a password again.
[ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519"
# Tidy up after an older installer, which on a second run could add the
# connection again: keep the first lynx-sdr, delete any others.
dupes=$(nmcli -g NAME,UUID con show 2>/dev/null | awk -F: '$1=="lynx-sdr"{print $2}' | tail -n +2)
for u in $dupes; do
  if sudo nmcli con delete uuid "$u" >/dev/null; then note "Removed a duplicate lynx-sdr connection"; fi
done
# Radxa's image gives every port NetworkManager's automatic DHCP connection
# ("Wired connection 2" on this one), which would fight lynx-sdr for the port.
# Those are removed; a connection someone set up by hand is left alone.
# (Read into a variable first: with pipefail, "loop | grep -q" fails.)
have_ours=0; theirs=""
while IFS=: read -r name uuid; do
  [ -n "$uuid" ] || continue
  if [ "$(nmcli -g connection.interface-name con show "$uuid" 2>/dev/null)" != "$SDR_PORT" ]; then continue; fi
  if [ "$name" = "lynx-sdr" ]; then have_ours=1
  elif [ "$(nmcli -g ipv4.method con show "$uuid" 2>/dev/null)" = "auto" ]; then
    if sudo nmcli con delete uuid "$uuid" >/dev/null; then note "Removed '$name', the automatic DHCP connection on $SDR_PORT"; fi
  else theirs="$name"
  fi
done < <(nmcli -g NAME,UUID con show 2>/dev/null)
if ! ip link show "$SDR_PORT" >/dev/null 2>&1; then
  warn "No port called $SDR_PORT: set up the direct link by hand (Installation Guide, Section 5.2), or re-run with LYNX_SDR_PORT=<name>."
elif [ "$have_ours" = 1 ]; then
  sudo nmcli con up lynx-sdr >/dev/null 2>&1 || true
  note "lynx-sdr already set up: ROCK 192.168.254.1, SDR gets 192.168.254.2"
elif [ -n "$theirs" ]; then
  warn "$SDR_PORT has your own connection '$theirs': left as it is. Lynx-Tx expects the SDR on its own link (Installation Guide, Section 5.2)."
else
  # The ROCK is 192.168.254.1 and hands the SDR 192.168.254.2 by DHCP, so
  # the SDR needs no setting up. One address in the pool: always .2.
  sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  echo "dhcp-range=192.168.254.2,192.168.254.2,12h" |
    sudo tee /etc/NetworkManager/dnsmasq-shared.d/lynx-sdr.conf >/dev/null
  sudo nmcli con add type ethernet ifname "$SDR_PORT" con-name lynx-sdr \
    ipv4.method shared ipv4.addresses 192.168.254.1/24 ipv6.method disabled >/dev/null
  sudo nmcli con up lynx-sdr >/dev/null 2>&1 || true
  note "lynx-sdr: ROCK 192.168.254.1, SDR gets 192.168.254.2"
fi

# --- Monitoring: SNMP for Home Assistant and the like ----------------
# mibs/LYNX-MIB.txt, served by snmpd through tools/lynx-snmp-agent.py from
# Lynx-Tx's /api/health. Read-only, community "lynx". Nothing on the
# transmit path depends on it.
step "Monitoring (SNMP)"
if sudo apt-get install -y snmpd >/dev/null 2>&1; then
  sudo install -D -m 755 "$APP_DIR/tools/lynx-snmp-agent.py" /usr/local/lib/lynx-tx/lynx-snmp-agent.py
  sudo install -D -m 644 "$APP_DIR/mibs/LYNX-MIB.txt" /usr/share/snmp/mibs/LYNX-MIB.txt
  # Debian's snmpd answers only the ROCK itself: open it to the network.
  sudo sed -i 's/^agentaddress .*/agentaddress udp:161,udp6:[::1]:161/' /etc/snmp/snmpd.conf
  sudo mkdir -p /etc/snmp/snmpd.conf.d
  sudo tee /etc/snmp/snmpd.conf.d/lynx.conf >/dev/null <<'SNMP'
view   lynxview  included  .1.3.6.1.2.1.1
view   lynxview  included  .1.3.6.1.4.1.8072.9999.1
rocommunity lynx default -V lynxview
pass_persist .1.3.6.1.4.1.8072.9999.1.1 /usr/bin/python3 /usr/local/lib/lynx-tx/lynx-snmp-agent.py
SNMP
  sudo systemctl enable snmpd >/dev/null 2>&1
  sudo systemctl restart snmpd && note "SNMP: community 'lynx', MIB in mibs/LYNX-MIB.txt (docs/lynx_tx_monitoring_guide.docx)"
else
  warn "Could not install snmpd: no SNMP monitoring."
fi

# --- 9. Service ------------------------------------------------
step "9/9 Service: start at boot"
sudo tee /etc/systemd/system/lynx-tx.service >/dev/null <<EOF
[Unit]
Description=Lynx-DATV-Tx
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/lynx_tx.py
Restart=on-failure
# Stop Lynx-Tx first (it mutes the SDR while IQ is still flowing), and only
# then the pipeline: never leave the SDR radiating a stale DAC buffer.
KillMode=mixed
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable lynx-tx >/dev/null 2>&1
sudo systemctl restart lynx-tx
sleep 3
systemctl is-active --quiet lynx-tx && note "lynx-tx running" || warn "lynx-tx did not start: journalctl -u lynx-tx -n 50"

# --- Done -------------------------------------------------------
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
echo
echo "=== Done ==="
echo "Open http://${IP:-<rock address>}:8080"
echo "Connect the SDR to the ROCK's second Ethernet port (far right) and power it: it appears on the Transmit card."
if [ ${#WARNINGS[@]} -gt 0 ]; then
  echo; echo "Finished with warnings:"; for w in "${WARNINGS[@]}"; do echo "  - $w"; done
fi
if [ "$CMA_REBOOT" = 1 ]; then
  echo
  echo "************************************************************"
  echo "  RESTART NEEDED: the 4K memory setting only takes effect"
  echo "  after a restart. Transmitting before it can freeze the ROCK."
  echo "************************************************************"
  # stdin is this script when run through curl | bash, so ask on the terminal.
  ans=""
  if [ -r /dev/tty ] && read -r -t 120 -p "Restart now? [Y/n] " ans </dev/tty; then :; else ans=""; echo; fi
  case "$ans" in
    [nN]*) echo "Then restart before transmitting: sudo reboot" ;;
    *) echo "Restarting. Give it a minute, then open http://${IP:-<rock address>}:8080"; sudo systemctl reboot ;;
  esac
fi
