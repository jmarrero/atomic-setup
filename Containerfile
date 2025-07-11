FROM quay.io/fedora/fedora-bootc:42

RUN mkdir -p /usr/lib/bootc/kargs.d
RUN echo 'kargs = ["radeon.si_support=0", "radeon.cik_support=0", "amdgpu.si_support=1", "amdgpu.cik_support=1", "amdgpu.dc=1"]' | \
    tee /usr/lib/bootc/kargs.d/00-amdgpu.toml > /dev/null

RUN \
    dnf -y install \
    # container tools
    podman toolbox \
    # local debug tools
    htop ripgrep  \
    # smb mounts
    cifs-utils \
    # preffered tools
    util-linux-user fish tuned && \
    # cleanup and verification stage
    dnf clean all && bootc container lint
