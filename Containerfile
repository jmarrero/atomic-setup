FROM quay.io/centos-bootc/centos-bootc:stream10
COPY /etc /etc
COPY /usr /usr
COPY . .
RUN \
    # git main bootc from copr
    dnf -y update bootc && \
    dnf -y group install "Workstation"
RUN dnf config-manager --set-enabled crb &&  dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm
RUN dnf -y install \
    # sign git tags for releases
    pinentry \
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
    sg3_utils \
    # Virt stack
    libvirt-daemon-config-network libvirt-daemon-kvm qemu-kvm \
    virt-install virt-manager virt-viewer virtiofsd 

#RUN #install & configure packages
RUN dnf install -y firewalld fwupd lm_sensors sysstat thermald tuned  xdpyinfo wget

#configure unit files
RUN systemctl enable lm_sensors sysstat tuned fstrim.timer podman.socket podman-auto-update.timer cockpit.socket libvirtd.socket && \
systemctl set-default graphical.target && bootc container lint


    # clean up
#    dnf clean all && rm -rf /var/* && rm -rf /workdir && bootc container lint
