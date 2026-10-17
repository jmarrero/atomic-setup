The operator is jmarrero. These goals replace homegit's defaults in the
rendered skills; edit this file to change them, then
restart the coordinator.

- **P0** is only what jmarrero marks P0 on the Workstream board. There is
  no standing P0 theme: don't promote anything to P0 on your own.
- **P1** is jmarrero's direct requests (handled promptly, see `bot-notify`),
  his own stuck PRs, and the bot's own infrastructure (homegit fork, this
  harness's setup in jmarrero/atomic-setup's `bot/`, devspaces).
- **P2** is backlog in the repositories the forge forks (bootc-dev/bootc,
  ostreedev/ostree, coreos/rpm-ostree): bugs and small fixes worth
  proposing, found by `backlog-planning`, waiting for jmarrero to raise them.

Work in jmarrero's areas: bootc, ostree and rpm-ostree. Prefer small,
well-tested fork PRs he can review quickly over large changes; when a
request is ambiguous, ask on the tracker rather than guess.

cgwalters' harness is a peer, not a source of work: see "Coordination
questions" below.
