# SPDX-License-Identifier: GPL-3.0-only
FROM archlinux:base-devel
RUN pacman -Syu --noconfirm --needed git meson ninja cmake pkgconf rust python \
    pyside6 qt6-multimedia qt6-svg qt6-wayland python-numpy python-opencv ffmpeg vulkan-headers \
    wayland wayland-protocols libdrm libglvnd libpulse libx11 libxcomposite \
    libxfixes libxext libxi libxcursor libxrandr libxrender libxdamage libxkbcommon \
    dbus pango freetype2 libdecor libva libcap pipewire fontconfig noto-fonts \
    && pacman -Scc --noconfirm
RUN useradd -m -u 1000 builder
COPY packaging/docker/build-arch.sh /usr/local/bin/build-layer-release
ENTRYPOINT ["bash", "/usr/local/bin/build-layer-release"]
