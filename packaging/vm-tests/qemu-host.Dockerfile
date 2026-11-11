# SPDX-License-Identifier: GPL-3.0-only
FROM buildpack-deps:trixie-curl

LABEL org.opencontainers.image.title="Layer QEMU distro VM host"
LABEL org.opencontainers.image.description="Loopback-only QEMU/KVM cloud-image test host; no GPU passthrough"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cloud-image-utils \
        curl \
        genisoimage \
        openssh-client \
        qemu-system-x86 \
        qemu-utils \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash layer \
    && mkdir -p /state /run/layer-vm \
    && chown layer:layer /state /run/layer-vm

USER 1000:1000
ENV HOME=/tmp
WORKDIR /state

# distro-vms.py passes qemu-system-x86_64, qemu-img, cloud-localds or
# ssh-keygen explicitly. /dev/kvm is supplied only at run time with
# --device /dev/kvm. No /dev/dri, vfio or NVIDIA devices are configured.
CMD ["qemu-system-x86_64", "--version"]
