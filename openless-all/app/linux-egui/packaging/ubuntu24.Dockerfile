# Build against the oldest supported GNOME/IBus distribution's libraries.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential clang cmake pkg-config curl ca-certificates nodejs git \
    libasound2-dev libdbus-1-dev libpipewire-0.3-dev libpulse-dev \
    libxkbcommon-dev libxkbcommon-x11-dev libwayland-dev libx11-dev \
    libx11-xcb-dev libegl1-mesa-dev libudev-dev libssl-dev liblzma-dev libbz2-dev \
    python3-gi gnome-settings-daemon-common desktop-file-utils \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.95.0 \
    && rm /tmp/rustup-init.sh
WORKDIR /src
