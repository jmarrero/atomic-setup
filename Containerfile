FROM quay.io/fedora/fedora-bootc:42
COPY /etc /etc
RUN \
    dnf5 -y install 'dnf5-command(copr)' && \
    dnf -y copr enable solopasha/hyprland && \
    dnf -y install \
    ### Hyprland and desktop apps
    kvantum-qt5 gnome-themes-extra Hyprland swaybg alacritty waybar \
    brightnessctl playerctl pamixer pavucontrol wireplumber \
    fcitx5 fcitx5-gtk fcitx5-qt fcitx5-configtool \
    nautilus sushi gnome-calculator \
    evince imv btop blueman-manager tuned hyprlock mako hypridle \
    # smb mounts
    cifs-utils \
    # wifi
    nmtui NetworkManager-wifi iwlwifi* \
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
# override default configs for Hyperland
# Hyprland -c /usr/share/hypr/hyprland.conf
COPY /usr /usr
RUN cd /usr/share/ && \
    git clone --single-branch --branch master https://github.com/basecamp/omarchy.git && \
    mv /usr/share/omarchy/config/waybar/* /etc/xdg/waybar/ && \
    sed -i '1s|@import "../omarchy/current/theme/waybar.css";|@import "/usr/share/omarchy/themes/tokyo-night/waybar.css";|' /etc/xdg/waybar/style.css && \
    sed -i 's|alacritty --class=Impala -e impala|alacritty -e nmtui|g' /etc/xdg/waybar/config.jsonc && \
    sed -i 's|blueberry|blueman-manager|g' /etc/xdg/waybar/config.jsonc
