FROM quay.io/fedora/fedora-bootc:42
COPY /etc /etc
COPY /usr /usr
RUN \
    dnf5 -y install 'dnf5-command(copr)' && \
    dnf -y copr enable solopasha/hyprland && \
    dnf -y install \
    ### Hyprland and desktop apps
    kvantum-qt5 gnome-themes-extra Hyprland swaybg alacritty waybar \
    brightnessctl playerctl pamixer pavucontrol wireplumber \
    fcitx5 fcitx5-gtk fcitx5-qt fcitx5-configtool \
    nautilus sushi gnome-calculator \
    evince imv btop blueman-manager tuned hyprlock mako \
    # smb mounts
    cifs-utils \
    # wifi
    nmtui NetworkManager-wifi \
    # container tools
    podman toolbox flatpak fedora-flathub-remote \
    # fonts
    fontawesome-fonts google-noto-sans-fonts google-noto-serif-fonts \
    google-noto-emoji-color-fonts "google-noto-sans-cjk-*-fonts" \
    # sign git tags for releases
    git-evtag pinentry \
    # system performance
    htop btop tuned \
    # dev tools
    ripgrep make xsel strace \
    # preffered tools
    util-linux-user fish \
    # tools I am learning
    tmux neovim \
    # logitech mouse/keyboard pairing & apple superdrive
    solaar sg3_utils && \
    # clean up
    dnf clean all && rm -rf /var/lib/unbound && rm -rf /workdir && bootc container lint
