FROM registry.fedoraproject.org/fedora-toolbox:45
RUN mkdir /development
WORKDIR /development
RUN \
    # RPM-OSTREE build deps & check it builds: https://github.com/coreos/rpm-ostree/blob/main/docs/HACKING.md
    git clone https://github.com/coreos/rpm-ostree.git && \
    cd /development/rpm-ostree && ./ci/installdeps.sh && \
    ./ci/install-cxx.sh && \
    export PATH=$PATH:/development/rpm-ostree/target/cargo-vendor-filterer/bin:/development/rpm-ostree/target/cxxbridge/bin && \
    git submodule update --init && \
    env CFLAGS='-ggdb -Og' CXXFLAGS='-ggdb -Og' ./autogen.sh --prefix=/usr --libdir=/usr/lib64 --sysconfdir=/etc && make && \
    # let's make super sure we have ostree & rpm-ostree build deps
    dnf builddep ostree rpm-ostree -y && \
    # Additional packages from repository
    dnf install -y \
         #Fedora Development tools
         fedora-packager fedora-review \
         #CentOS development tools
         centpkg \
         #Install other tools I use:
         ripgrep xsel tmate strace clang-tools-extra tmux neovim nu \
         #rust-src is needed when you are not using rust-up to install rustc to be able to use rust-analyzer
         rust-src rust-analyzer rustfmt \
         util-linux-user fish && \
         # Install Coreos Assembler: https://coreos.github.io/coreos-assembler/devel/
         cd /development && git clone https://github.com/coreos/coreos-assembler.git && \
         cd /development/coreos-assembler && \
         ./build.sh configure_yum_repos && ./build.sh install_rpms && make && make install && \
         #Cleanup 
         cd / && dnf clean all && rm -rf /development
