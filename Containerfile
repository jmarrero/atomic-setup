FROM quay.io/centos-bootc/centos-bootc:stream10
COPY /usr /usr
RUN dnf install -y https://download.copr.fedorainfracloud.org/results/rhcontainerbot/bootc/fedora-42-x86_64/09436359-bootc/bootc-202508161401.g1d5974e6cd-1.fc42.x86_64.rpm
RUN \
sudo dnf -y install \
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
