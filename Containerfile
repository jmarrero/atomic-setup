FROM quay.io/fedora/fedora-coreos:testing
RUN dnf -y install \
    # sign git tags for releases
    git-evtag pinentry \
    # local debug tools 
    htop ripgrep  \
    # kerberos auth
    krb5-workstation \
    # dev tools
    make xsel strace \
    # preffered tools
    tmux neovim
