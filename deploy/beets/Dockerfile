# beets image with the beets-hermes plugin. Build from the Hermes repo root:
#   docker build -f deploy/beets/Dockerfile -t <registry>/beets-hermes:<tag> .
#
# Contract this image must satisfy, whatever the base image is:
#   1. `beet` works with BEETSDIR pointing at the config directory.
#   2. `hermes-agent` is on PATH (pip installs it next to `beet`; the symlink below makes
#      that true even when the base image's entrypoint is bypassed).
#   3. `beets-python` on PATH is the interpreter that owns `beet` (used by dev scripts).
# Swap the FROM line for any other beets image and the rest still applies.
#
# The tag names the beets release and the digest pins the exact build; Dependabot
# (.github/dependabot.yml) opens a PR when linuxserver publishes a new tag, and the images
# workflow runs the plugin's tests inside the resulting image before pushing it. The pushed
# image is tagged with the beets version it carries (`beet version`), not with Hermes's.
FROM lscr.io/linuxserver/beets:2.14.0@sha256:bbd09e2b19a0903f3f916c26e9ae5c30004913b293228b422154c0f9fecb2cd9

COPY beets-hermes /tmp/beets-hermes
RUN set -eu; \
    # Use the same interpreter that owns `beet`, so the plugin lands in beets' environment.
    BEET="$(command -v beet || ls /lsiopy/bin/beet /usr/local/bin/beet 2>/dev/null | head -1)"; \
    BIN="$(dirname "$BEET")"; \
    "$BIN/python" -m pip install --no-cache-dir /tmp/beets-hermes; \
    ln -sf "$BIN/hermes-agent" /usr/local/bin/hermes-agent; \
    ln -sf "$BEET" /usr/local/bin/beet; \
    # A wrapper, not a symlink: Python finds its venv from the invoked path, so a symlink
    # outside the venv would start the system interpreter without beets.
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$BIN/python" > /usr/local/bin/beets-python; \
    chmod +x /usr/local/bin/beets-python; \
    rm -rf /tmp/beets-hermes
