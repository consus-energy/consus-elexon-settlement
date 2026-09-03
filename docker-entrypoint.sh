#!/bin/sh
# Import the signing keys, then run the command.
#
# The keyring is built at startup from secrets rather than baked into the
# image. An image containing a private key is a private key in Artifact
# Registry, readable by anyone who can pull the image, and present in every
# layer cache that ever held it.
#
# Secrets arrive as files, mounted by Cloud Run. Files rather than environment
# variables because a multi-line armoured key does not survive an environment
# variable cleanly, and because environment variables are visible to anything
# that can read /proc.

set -eu

: "${CONSUS_GNUPGHOME:=/home/gateway/.gnupg}"
export GNUPGHOME="$CONSUS_GNUPGHOME"

# gpg refuses a home directory that others can read, and says so unclearly.
chmod 700 "$GNUPGHOME"

if [ -f "${CONSUS_GPG_PRIVATE_KEY_FILE:-}" ]; then
    # --batch and loopback pinentry because there is no terminal here to
    # prompt on. Import is idempotent: a restart re-imports the same key and
    # gpg recognises it rather than duplicating.
    gpg --batch --yes --pinentry-mode loopback \
        --passphrase-file "$CONSUS_GPG_PASSPHRASE_FILE" \
        --import "$CONSUS_GPG_PRIVATE_KEY_FILE"

    # Ultimate trust on our own key. Without it gpg warns on every signature,
    # which buries real warnings in noise.
    printf '%s:6:\n' "$(gpg --batch --with-colons --list-secret-keys \
        | awk -F: '/^fpr:/ {print $10; exit}')" \
        | gpg --batch --import-ownertrust
else
    echo "WARNING: no private key configured. The gateway can build files but" >&2
    echo "         cannot sign or send them." >&2
fi

if [ -f "${CONSUS_GPG_RECIPIENT_KEY_FILE:-}" ]; then
    gpg --batch --yes --import "$CONSUS_GPG_RECIPIENT_KEY_FILE"
fi

exec consus-settlement "$@"