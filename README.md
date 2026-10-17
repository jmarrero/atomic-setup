This is my Fedora Kinoite 45 bootc setup (Bootable Containers) for a
2013 Mac Pro 6,1.

The image includes the RPM Fusion Broadcom `wl` driver for the Mac Pro's
BCM4360 (`14e4:43a0`) Wi-Fi adapter. The module is built against the exact
kernel contained in the image during the container build.

The setup builds the ./Containerfile using buildah.

The resulting build is pushed to:
ghcr.io/jmarrero/bootc-macpro61tc:latest

## bot/

`bot/` is the agent bot this machine runs: an @mention responder (botd), a
coordinator running [homegit](https://github.com/jmarrero-forge/homegit)'s
harness, and the setup for its forge org and devspaces. Start with
[bot/SETUP.md](bot/SETUP.md), which also says what to change to set it up
for someone else. It isn't part of the OS image.
