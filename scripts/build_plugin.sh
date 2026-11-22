#!/usr/bin/env bash
# Build the DSH plugin tarball (no secrets inside; config is read at runtime).
set -euo pipefail
cd "$(dirname "$0")/../plugin"
npm pack --silent
ls -lh ./*.tgz
