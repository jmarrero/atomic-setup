FROM quay.io/fedora/fedora-silverblue:42
COPY /etc /etc
COPY /usr /usr
RUN \
sudo dnf -y install \
    # git main bootc from copr
    bootc \
    # smb mounts
    cifs-utils \
    # system performance
    btop tuned \
    # search tool
    ripgrep \
    # preffered tools
    util-linux-user fish && \
    # clean up
    dnf clean all && rm -rf /var/* && rm -rf /workdir && bootc container lint
