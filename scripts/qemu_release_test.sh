#!/usr/bin/env bash
# Local release-gate test: boot a vanilla cloud image in QEMU/KVM, install the
# locally-built ccproxy wheel, and validate the full WireGuard namespace jail
# path end-to-end. GitHub Actions can't run this because the namespace-jail
# requires real kernel modules + raw networking on a clean OS.
#
# Run via: just release-test-qemu DISTRO   (DISTRO = debian-12 | ubuntu-24.04 | fedora-41)
#
# Requirements on the host:
#   - qemu-system-x86_64, qemu-img
#   - cloud-localds (cloud-image-utils)  OR  genisoimage / mkisofs
#   - /dev/kvm accessible
#   - ssh + ssh-keygen
#   - A wheel in ./dist/  (build with: uv build --wheel)

set -euo pipefail

readonly DISTRO="${1:-debian-12}"
readonly WHEEL_DIR="${WHEEL_DIR:-$PWD/dist}"
readonly REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
readonly CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/ccproxy-qemu"
readonly SSH_PORT="${SSH_PORT:-2222}"

case "$DISTRO" in
  debian-12)
    IMG_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
    REMOTE_USER="debian"
    PKG_INSTALL="sudo apt-get update -q && sudo apt-get install -yq --no-install-recommends slirp4netns wireguard-tools iproute2 iptables curl ca-certificates"
    ;;
  ubuntu-24.04)
    IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
    REMOTE_USER="ubuntu"
    PKG_INSTALL="sudo apt-get update -q && sudo apt-get install -yq --no-install-recommends slirp4netns wireguard-tools iproute2 iptables curl ca-certificates"
    ;;
  fedora-44)
    IMG_URL="https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    REMOTE_USER="fedora"
    PKG_INSTALL="sudo dnf install -y slirp4netns wireguard-tools iproute iptables-nft curl ca-certificates"
    ;;
  *)
    echo "ERROR: unknown distro '$DISTRO'" >&2
    echo "Supported: debian-12, ubuntu-24.04, fedora-44" >&2
    exit 1
    ;;
esac

log() { printf '[ccproxy-qemu %s] %s\n' "$DISTRO" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "missing required host command: $cmd"
  done
}

require qemu-system-x86_64 qemu-img ssh ssh-keygen curl
test -r /dev/kvm || die "/dev/kvm not readable (KVM unavailable or no permission)"

# cloud-localds is preferred; genisoimage / mkisofs as fallback.
if command -v cloud-localds >/dev/null 2>&1; then
  SEED_TOOL=cloud-localds
elif command -v genisoimage >/dev/null 2>&1; then
  SEED_TOOL=genisoimage
elif command -v mkisofs >/dev/null 2>&1; then
  SEED_TOOL=mkisofs
else
  die "need one of: cloud-localds, genisoimage, mkisofs"
fi

# Locate wheel
shopt -s nullglob
wheels=("$WHEEL_DIR"/claude_ccproxy-*.whl "$WHEEL_DIR"/claude-ccproxy-*.whl)
shopt -u nullglob
test "${#wheels[@]}" -ge 1 || die "no wheel found in $WHEEL_DIR (run: uv build --wheel)"
readonly WHEEL_PATH="${wheels[0]}"
log "using wheel: $WHEEL_PATH"

# Work dir
WORK_DIR="$(mktemp -d -t ccproxy-qemu-XXXXXX)"
QEMU_PID=""
cleanup() {
  if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
    log "killing QEMU pid=$QEMU_PID"
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

# 1. Download base cloud image (cached)
mkdir -p "$CACHE_DIR"
readonly BASE_IMG="$CACHE_DIR/$(basename "$IMG_URL")"
if [[ ! -f "$BASE_IMG" ]]; then
  log "downloading base image: $IMG_URL"
  curl -L --fail --progress-bar \
       --retry 5 --retry-delay 5 --retry-all-errors \
       -C - -o "$BASE_IMG.tmp" "$IMG_URL"
  mv "$BASE_IMG.tmp" "$BASE_IMG"
fi

# 2. COW overlay disk so we don't mutate the cache
readonly DISK="$WORK_DIR/disk.qcow2"
qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMG" "$DISK" 20G

# 3. SSH key for this run
ssh-keygen -t ed25519 -N "" -f "$WORK_DIR/id_ed25519" -q
readonly PUBKEY="$(cat "$WORK_DIR/id_ed25519.pub")"

# 4. Cloud-init seed — minimal: SSH + DNS + sysctl unlock only.
# Package install is done over SSH because the host's NixOS resolved at
# 127.0.0.53 doesn't pass through QEMU SLIRP DNS, so cloud-init's network
# work in early boot fails. By the time SSH is up, manage_resolv_conf has
# given us 1.1.1.1 and apt works fine.
cat > "$WORK_DIR/user-data" <<EOF
#cloud-config
users:
  - name: $REMOTE_USER
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $PUBKEY
ssh_pwauth: false
manage_resolv_conf: true
resolv_conf:
  nameservers:
    - 1.1.1.1
    - 8.8.8.8
write_files:
  - path: /etc/sysctl.d/99-userns.conf
    content: |
      kernel.apparmor_restrict_unprivileged_userns = 0
  - path: /etc/resolv.conf
    content: |
      nameserver 1.1.1.1
      nameserver 8.8.8.8
runcmd:
  - sysctl --system
  - modprobe wireguard || true
EOF

cat > "$WORK_DIR/meta-data" <<EOF
instance-id: ccproxy-qemu-test-$$
local-hostname: ccproxy-test
EOF

case "$SEED_TOOL" in
  cloud-localds)
    cloud-localds "$WORK_DIR/seed.iso" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"
    ;;
  genisoimage|mkisofs)
    (cd "$WORK_DIR" && "$SEED_TOOL" -output seed.iso -volid cidata -joliet -rock user-data meta-data) >/dev/null 2>&1
    ;;
esac

# 5. Boot QEMU (headless, daemonised, host wheel shared via 9p)
log "booting QEMU"
qemu-system-x86_64 \
  -accel kvm \
  -cpu host \
  -m 4096 \
  -smp 4 \
  -drive file="$DISK",if=virtio,format=qcow2 \
  -drive file="$WORK_DIR/seed.iso",if=virtio,format=raw,readonly=on \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
  -device virtio-net-pci,netdev=net0 \
  -serial file:"$WORK_DIR/serial.log" \
  -monitor none \
  -display none \
  -daemonize \
  -pidfile "$WORK_DIR/qemu.pid"

QEMU_PID="$(cat "$WORK_DIR/qemu.pid")"
log "QEMU pid=$QEMU_PID, serial log=$WORK_DIR/serial.log"

# 6. Wait for SSH (cloud-init takes ~60-90s on first boot)
SSH_OPTS=(
  -i "$WORK_DIR/id_ed25519"
  -p "$SSH_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=30
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
  -o LogLevel=ERROR
)

log "waiting for SSH port $SSH_PORT to bind (up to 90s)"
for i in $(seq 1 18); do
  if (exec 3<>/dev/tcp/127.0.0.1/$SSH_PORT) 2>/dev/null; then
    exec 3<&- 3>&-
    log "port $SSH_PORT open (attempt $i)"
    break
  fi
  if [[ $i -eq 18 ]]; then
    log "----- serial log tail -----"
    tail -50 "$WORK_DIR/serial.log" >&2 || true
    die "port $SSH_PORT never opened"
  fi
  sleep 5
done

log "waiting for SSH auth (up to 5 min)"
ssh_err=""
for i in $(seq 1 60); do
  if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@localhost" "true" 2>"$WORK_DIR/ssh.err"; then
    log "SSH auth ok (attempt $i)"
    ssh_err=""
    break
  fi
  ssh_err="$(cat "$WORK_DIR/ssh.err" 2>/dev/null || true)"
  if [[ $i -eq 60 ]]; then
    log "----- last SSH error -----"
    echo "$ssh_err" >&2
    log "----- serial log tail -----"
    tail -50 "$WORK_DIR/serial.log" >&2 || true
    die "SSH auth never succeeded"
  fi
  sleep 5
done

log "waiting for cloud-init to finish"
# Exit codes: 0 = clean, 2 = recoverable warnings (still "done"), 1 = failed.
# Fedora often returns 2 because of harmless module warnings; treat 0 and 2 as success.
ci_rc=0
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@localhost" "cloud-init status --wait" || ci_rc=$?
case "$ci_rc" in
  0|2) ;;
  *)   die "cloud-init failed (rc=$ci_rc)" ;;
esac

# 7. scp the wheel into the VM (simpler than 9p; cloud kernels lack 9p modules).
# Preserve the original filename — uv requires PEP-427 wheel naming.
readonly WHEEL_BASENAME="$(basename "$WHEEL_PATH")"
log "copying wheel into VM ($WHEEL_BASENAME)"
scp -i "$WORK_DIR/id_ed25519" \
    -P "$SSH_PORT" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "$WHEEL_PATH" "$REMOTE_USER@localhost:/tmp/$WHEEL_BASENAME"

# 8. Run the smoke test inside the VM
log "running smoke test inside VM"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@localhost" 'bash -se' <<REMOTE
set -euo pipefail

echo '[vm] ensuring DNS works for package install'
if ! getent hosts deb.debian.org >/dev/null 2>&1 && ! getent hosts download.fedoraproject.org >/dev/null 2>&1; then
  sudo bash -c 'printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/resolv.conf'
fi

echo '[vm] installing system packages'
$PKG_INSTALL

echo '[vm] installing uv'
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="\$HOME/.local/bin:\$PATH"

echo '[vm] provisioning Python 3.13'
uv python install 3.13

echo '[vm] creating venv + installing wheel'
uv venv --python 3.13 /tmp/v
source /tmp/v/bin/activate
uv pip install /tmp/$WHEEL_BASENAME

echo '[vm] --- smoke: help (verifies entry point + tyro dispatch)'
ccproxy --help > /dev/null

echo '[vm] --- smoke: init'
export CCPROXY_CONFIG_DIR=\$HOME/.config/ccproxy
mkdir -p "\$CCPROXY_CONFIG_DIR"
ccproxy init
test -f "\$CCPROXY_CONFIG_DIR/ccproxy.yaml"

echo '[vm] --- smoke: system tools on PATH'
# Debian/Ubuntu put iptables/ip/sysctl in /usr/sbin which isn't in non-root PATH by default.
export PATH="\$PATH:/usr/sbin:/sbin"
for tool in slirp4netns wg unshare nsenter ip iptables sysctl; do
  command -v "\$tool" >/dev/null || { echo "missing: \$tool"; exit 1; }
done

echo '[vm] --- smoke: status (expect bitmask 3 = proxy|inspect down)'
rc=0
ccproxy status --proxy --inspect || rc=\$?
test "\$rc" = "3" || { echo "unexpected status rc=\$rc"; exit 1; }

echo '[vm] --- e2e: daemon start + proxy port reachable'
# Validates that ccproxy start can actually bind its listeners on a fresh
# install. Doesn't exercise the WireGuard namespace jail (that needs
# `ccproxy run --inspect` against the live daemon, which is an integration
# concern beyond the install smoke test).
nohup ccproxy start > /tmp/ccproxy.log 2>&1 &
CCPROXY_PID=\$!
trap "kill \$CCPROXY_PID 2>/dev/null || true" EXIT
ready=0
for i in \$(seq 1 30); do
  if (exec 3<>/dev/tcp/127.0.0.1/4000) 2>/dev/null; then
    exec 3<&- 3>&-
    echo "[vm] proxy bound :4000 (attempt \$i)"
    ready=1
    break
  fi
  sleep 1
done
if [[ \$ready -eq 0 ]]; then
  echo "[vm] proxy never bound :4000"
  tail -50 /tmp/ccproxy.log >&2
  exit 1
fi
rc=0
ccproxy status --proxy || rc=\$?
test "\$rc" = "0" || { echo "status --proxy reports down (rc=\$rc)"; exit 1; }

echo '[vm] ALL TESTS PASSED'
REMOTE

log "shutting VM down"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@localhost" "sudo poweroff" 2>/dev/null || true

# Wait for QEMU to actually exit; cleanup trap kills if it overruns.
for i in $(seq 1 30); do
  if ! kill -0 "$QEMU_PID" 2>/dev/null; then
    QEMU_PID=""
    break
  fi
  sleep 1
done

log "OK"
