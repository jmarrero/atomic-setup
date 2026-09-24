ARG FEDORA_VERSION=45

# Build Broadcom's out-of-tree wl module against the kernel shipped in the
# image.  uname -r cannot be used here because it reports the build host's
# kernel rather than the target image's kernel.
FROM quay.io/fedora/silverblue-kinoite:${FEDORA_VERSION} AS wl-builder
ARG FEDORA_VERSION
RUN \
    dnf -y install \
        "https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-${FEDORA_VERSION}.noarch.rpm" && \
    kver=$(rpm -q kernel-core --queryformat '%{VERSION}-%{RELEASE}.%{ARCH}') && \
    # Fedora Atomic desktop composes can contain a kernel that is still in updates-testing.
    # Enable it only for the exact matching kernel-devel package.
    dnf -y --enablerepo=updates-testing install \
        akmods "kernel-devel-${kver}" && \
    # Fedora 44's akmod-wl %post currently invokes akmodsbuild as root and
    # makes the transaction fatal. Install its payload without that broken
    # automatic build, then run akmods explicitly below.
    dnf -y --setopt=tsflags=noscripts install akmod-wl && \
    akmods --force --kernels "${kver}" && \
    test -n "$(find /var/cache/akmods/wl -maxdepth 1 -name 'kmod-wl-*.rpm' -print -quit)"

FROM quay.io/fedora/fedora-silverblue:${FEDORA_VERSION}
ARG FEDORA_VERSION
COPY /etc /etc
COPY /usr /usr
COPY . .
COPY --from=wl-builder /var/cache/akmods/wl/kmod-wl-*.rpm /tmp/wl-kmods/
RUN \
    # git main bootc from copr
    dnf -y update bootc && \
    # Broadcom BCM4360 Wi-Fi driver and the matching pre-built kernel module
    dnf -y install \
        "https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-${FEDORA_VERSION}.noarch.rpm" && \
    dnf -y install /tmp/wl-kmods/kmod-wl-*.rpm && \
    kver=$(rpm -q kernel-core --queryformat '%{VERSION}-%{RELEASE}.%{ARCH}') && \
    depmod -a "${kver}" && modinfo -k "${kver}" wl >/dev/null && \
    dnf -y install \
    # sign git tags for releases
    git-evtag pinentry \
    # kerberos auth
    krb5-workstation \
    # smb mounts
    cifs-utils \
    # system performance
    btop tuned \
    # search tool
    ripgrep \
    # preffered tools
    util-linux-user fish make xsel tmux neovim \
    # logitech mouse/keyboard pairing & apple superdrive
    solaar sg3_utils \
    # Virt stack
    libvirt-daemon-config-network libvirt-daemon-kvm qemu-kvm libguestfs-tools virt-resize \
    genisoimage virt-install virt-manager virt-viewer virtiofsd && \
    # clean up
    dnf clean all && rm -rf /tmp/wl-kmods /var/* && \
    # Rebuild initramfs with ostree, lvm, crypt modules and thunderbolt udev rule
    mkdir -p /var/tmp && \
    kver=$(ls /usr/lib/modules) && \
    dracut --verbose --force --reproducible \
        --install "/etc/udev/rules.d/98-thunderbolt.rules" \
        "/usr/lib/modules/${kver}/initramfs.img" "${kver}" && \
    rm -rf /var/tmp && rm -rf /workdir && bootc container lint
