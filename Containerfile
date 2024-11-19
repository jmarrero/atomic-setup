FROM quay.io/fedora/fedora-silverblue:41
RUN \
    ## Add vscode repo and key
    rpm --import https://packages.microsoft.com/keys/microsoft.asc && \
    sudo sh -c 'echo -e "[code]\nname=Visual Studio Code\nbaseurl=https://packages.microsoft.com/yumrepos/vscode\nenabled=1\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc" > /etc/yum.repos.d/vscode.repo' && \
    dnf -y install \
    # sign git tags for releases
    git-evtag pinentry \
    # local debug tools 
    htop ripgrep  \
    # kerberos auth
    krb5-workstation \
    # run local qemu vms
    libvirt-daemon-config-network libvirt-daemon-kvm qemu-kvm \
    virt-install virt-manager virt-viewer \
    # podman machine and reqs
    podman-gvproxy virtiofsd \
    # dev tools
    make xsel strace \
    # preffered tools
    util-linux-user fish tmux neovim code \
    # logitech mouse/keyboard pairing & apple superdrive
    solaar sg3_utils \
    # add podman-machine for podman-bootc
    podman-machine && \
    # Install podman-bootc thru copr
    dnf -y install 'dnf-command(copr)' && \
    dnf -y copr enable gmaglione/podman-bootc && \
    dnf -y install podman-bootc
