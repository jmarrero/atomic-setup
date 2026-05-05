FROM quay.io/fedora/fedora-silverblue:44
COPY /etc /etc
COPY /usr /usr
COPY . .
RUN \
    # git main bootc from copr
    dnf -y update bootc && \
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
    util-linux-user zsh make xsel tmux neovim \
    # logitech mouse/keyboard pairing & apple superdrive
    solaar sg3_utils \
    # Virt stack
    libvirt-daemon-config-network libvirt-daemon-kvm qemu-kvm \
    virt-install virt-manager virt-viewer virtiofsd && \
    # clean up
    dnf clean all && rm -rf /var/* && \
    # Rebuild initramfs with ostree, lvm, crypt modules and thunderbolt udev rule
    mkdir -p /var/tmp && \
    kver=$(ls /usr/lib/modules) && \
    dracut --verbose --force --reproducible \
        --install "/etc/udev/rules.d/98-thunderbolt.rules" \
        "/usr/lib/modules/${kver}/initramfs.img" "${kver}" && \
    rm -rf /var/tmp && rm -rf /workdir && bootc container lint
