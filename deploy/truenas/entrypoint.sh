#!/usr/bin/env bash
# The container runs as the dataset's owning uid (see `user:` in the compose
# file), which does not exist in the image. OpenSSH calls getpwuid() and refuses
# to start without a passwd entry -- "No user exists for uid NNNN" -- which kills
# the rsync pull.
#
# So synthesise one. /etc/passwd is made group-writable in the Dockerfile and the
# container runs with gid 0, which is what lets a non-root user append to it.
# Doing this at runtime rather than baking a uid into the image keeps the uid a
# deployment detail: change INFRA_UID and nothing needs rebuilding.
set -u
if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
    echo "infra:x:$(id -u):$(id -g):infra:${HOME:-/tmp}:/bin/bash" >> /etc/passwd 2>/dev/null \
        || echo "warning: could not add a passwd entry for uid $(id -u); ssh may refuse to run" >&2
fi
exec "$@"
