#!/usr/bin/env bash
set -euo pipefail
APP_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${OPENLESS_UBUNTU24_OUTPUT:-"$APP_ROOT/target/ubuntu24"}
mkdir -p "$OUTPUT"
OUTPUT=$(cd "$OUTPUT" && pwd)
IMAGE=openless-ubuntu24-build:local
docker build --platform linux/amd64 -t "$IMAGE" \
  -f "$APP_ROOT/linux-egui/packaging/ubuntu24.Dockerfile" "$APP_ROOT/linux-egui/packaging"
docker run --rm --platform linux/amd64 \
  --mount "type=bind,src=$APP_ROOT,dst=/src,readonly" \
  --mount "type=bind,src=$OUTPUT,dst=/build" \
  -e CARGO_TARGET_DIR=/build -e CARGO_HOME=/build/cargo \
  -e CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}" \
  -e BUILD_UID="$(id -u)" -e BUILD_GID="$(id -g)" \
  "$IMAGE" bash -ec '
    cargo build --locked --release -p openless-linux-egui
    /usr/bin/python3 linux-egui/tests/portal_bridge_test.py
    export OPENLESS_LINUX_INPUT_BACKEND=portal OPENLESS_LINUX_GLIBC_MAX=2.39
    export OPENLESS_LINUX_VERSION="$(node -p "require(\"./package.json\").version.split(\"+\")[0]")-$(cat linux-egui/package-revision)"
    bash scripts/package-linux-egui.sh
    cd /build/linux-egui-packages
    sha256sum ./*.deb > SHA256SUMS
    chown -R "$BUILD_UID:$BUILD_GID" /build/linux-egui-packages
  '
