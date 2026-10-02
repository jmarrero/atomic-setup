# Image for bootc-bot job containers: one disposable container per request.
# Build: podman build -t localhost/bootc-bot-agent -f bot/agent.Containerfile bot/
FROM registry.fedoraproject.org/fedora:45

RUN dnf install -y \
        # basics for reading and searching code
        git gh jq ripgrep fd-find diffutils patch findutils procps-ng python3 \
        # to push branches to and run builds on devspace runners over the tailnet
        openssh-clients rsync tmux just \
        # so agents can build and test Rust projects like bootc
        cargo rust clippy rustfmt gcc make pkgconf-pkg-config \
        # bootc/ostree tooling to inspect behavior, man pages and images
        bootc ostree skopeo man-db \
        # runtime for the agent CLIs
        nodejs npm && \
    dnf clean all

RUN npm install -g @anthropic-ai/claude-code @openai/codex @github/copilot opencode-ai && \
    npm cache clean --force

# The bot's commit identity, for any commit made in a job.
RUN git config --system user.name "Joseph Marrero" && \
    git config --system user.email "jmarrero+bot@gmail.com"

# botd runs jobs as uid 2000, mapped to the host user with --userns=keep-id
RUN useradd -m -u 2000 -s /bin/bash agent
USER agent
WORKDIR /home/agent
# Bind mounts must not cause Podman to create these as root-owned directories.
RUN mkdir -m 700 /home/agent/.codex /home/agent/.claude
