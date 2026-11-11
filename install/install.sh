#!/usr/bin/env bash
# Installs studio-bridge on a Raspberry Pi (Raspberry Pi OS / CompanionPi, bookworm or newer).
#   sudo ./install/install.sh            everything
#   sudo ./install/install.sh --no-kmod  skip the EVO kernel module
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KMOD=1
[ "${1:-}" = "--no-kmod" ] && KMOD=0

# The Godox mesh library (MIT), pinned to the commit this bridge was written against (tag v0.3.3).
GODOX_LIB="godox-mesh-bt @ git+https://github.com/binary-person/ha-godox-mesh@11002dd7e6474a132f2260f3e4abd2574878f327"

echo "== packages"
apt-get update -q
apt-get install -y -q python3-venv python3-dev git bluez build-essential

echo "== user"
id studio-bridge >/dev/null 2>&1 || useradd --system --home-dir /var/lib/studio-bridge --shell /usr/sbin/nologin studio-bridge
usermod -aG bluetooth studio-bridge
install -d -o studio-bridge -g studio-bridge -m 0700 /var/lib/studio-bridge

echo "== bridge"
install -d /opt/studio-bridge
[ -x /opt/studio-bridge/venv/bin/python ] || python3 -m venv /opt/studio-bridge/venv
/opt/studio-bridge/venv/bin/pip install -q --upgrade pip
/opt/studio-bridge/venv/bin/pip install -q "$GODOX_LIB" pyusb
/opt/studio-bridge/venv/bin/pip install -q --force-reinstall --no-deps "$HERE/bridge"
/opt/studio-bridge/venv/bin/pip install -q "$HERE/bridge"

# `studio-bridge ...` from any shell runs as the service user, so the state files stay its own.
cat > /usr/local/bin/studio-bridge <<'WRAP'
#!/bin/sh
if [ "$(id -un)" = studio-bridge ]; then exec /opt/studio-bridge/venv/bin/studio-bridge "$@"; fi
exec sudo -u studio-bridge -g studio-bridge /opt/studio-bridge/venv/bin/studio-bridge "$@"
WRAP
chmod 755 /usr/local/bin/studio-bridge

install -d /etc/studio-bridge
[ -f /etc/studio-bridge/config.toml ] || install -m 0644 "$HERE/install/config.example.toml" /etc/studio-bridge/config.toml
install -m 0644 "$HERE/install/60-studio-bridge.rules" /etc/udev/rules.d/60-studio-bridge.rules

if [ "$KMOD" = 1 ]; then
	echo "== EVO kernel module"
	# Raspberry Pi OS names its header packages after the board; try what exists.
	apt-get install -y -q dkms
	apt-get install -y -q "linux-headers-$(uname -r)" 2>/dev/null \
		|| apt-get install -y -q linux-headers-rpi-2712 2>/dev/null \
		|| apt-get install -y -q raspberrypi-kernel-headers 2>/dev/null \
		|| echo "!! no kernel headers package found; the module build below may fail"
	dkms remove evo_ctl/0.1 --all >/dev/null 2>&1 || true
	rm -rf /usr/src/evo_ctl-0.1
	install -d /usr/src/evo_ctl-0.1
	install -m 0644 "$HERE"/kmod/evo_ctl/{evo_ctl.c,Makefile,dkms.conf} /usr/src/evo_ctl-0.1/
	if dkms add evo_ctl/0.1 && dkms build evo_ctl/0.1 && dkms install evo_ctl/0.1; then
		modprobe evo_ctl || true
	else
		echo "!! The kernel module did not build. The lights are unaffected."
		echo "!! See docs: either fix the headers and rerun, or set transport = \"libusb\"."
	fi
fi

udevadm control --reload
udevadm trigger

echo "== service"
install -m 0644 "$HERE/install/studio-bridge.service" /etc/systemd/system/studio-bridge.service
systemctl daemon-reload
systemctl enable studio-bridge >/dev/null
systemctl restart studio-bridge

echo
echo "Installed. Next:"
echo "  1. Add the lights (README, 'Add the lights')."
echo "  2. Import dist/studio-godox-*.tgz and dist/studio-evo-*.tgz in Companion (Modules > Import module package)."
[ -e /dev/evo8 ] && echo "  EVO 8 found at /dev/evo8." || echo "  EVO 8 not seen yet (/dev/evo8 missing): plug it in, then 'ls /dev/evo8'."
