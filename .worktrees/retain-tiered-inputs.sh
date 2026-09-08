#!/usr/bin/env bash
set -eu
tar -czf /home/weitianc/clawbox-tiered-recording-20260907.tar.gz -C / \
  data/recording-evidence data/recording-output \
  home/weitianc/ClawBox/results/paper_replay_20260901_128g_v4/selected-traces \
  home/weitianc/ClawBox-tiered-2906a91/examples/predictions
git -C /home/weitianc/CubeSandbox-tiered-20260907-v4 bundle create /home/weitianc/cubesandbox-tiered-094daaa.bundle --all
sha256sum /home/weitianc/clawbox-tiered-recording-20260907.tar.gz /home/weitianc/cubesandbox-tiered-094daaa.bundle
