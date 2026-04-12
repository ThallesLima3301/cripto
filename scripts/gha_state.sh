#!/usr/bin/env bash
# scripts/gha_state.sh — encrypted state pull/push for GitHub Actions.
#
# The crypto_monitor SQLite database is the sole piece of durable state.
# On GitHub Actions runners are ephemeral, so each workflow run must:
#
#   1. pull  — decrypt the DB from the state branch into data/
#   2. (run the CLI command — handled by the workflow, not this script)
#   3. push  — checkpoint WAL, encrypt, verify, commit + force-push
#
# The state branch is kept at exactly ONE commit (--amend + --force-with-lease)
# so the repo never accumulates binary history.
#
# Encryption: AES-256-CBC with PBKDF2 key derivation via openssl.
# The passphrase comes from the STATE_ENCRYPTION_KEY GitHub Secret.
#
# Usage:
#   STATE_ENCRYPTION_KEY=... bash scripts/gha_state.sh pull
#   STATE_ENCRYPTION_KEY=... bash scripts/gha_state.sh push

set -euo pipefail

CMD="${1:?Usage: gha_state.sh pull|push}"

DB_PATH="data/crypto_monitor.db"
STATE_DIR=".state"
ENC_FILE="$STATE_DIR/crypto_monitor.db.enc"

# ── validate key ──────────────────────────────────────────────────
: "${STATE_ENCRYPTION_KEY:?STATE_ENCRYPTION_KEY is not set — add it as a GitHub Secret}"

if [ "${#STATE_ENCRYPTION_KEY}" -lt 16 ]; then
    echo "::error::STATE_ENCRYPTION_KEY is too short (min 16 chars)" >&2
    exit 1
fi

# ── helpers ───────────────────────────────────────────────────────
encrypt() {
    openssl enc -aes-256-cbc -pbkdf2 -salt \
        -pass "pass:${STATE_ENCRYPTION_KEY}" \
        -in "$1" -out "$2"
}

decrypt() {
    openssl enc -d -aes-256-cbc -pbkdf2 \
        -pass "pass:${STATE_ENCRYPTION_KEY}" \
        -in "$1" -out "$2"
}

db_size() {
    wc -c < "$1" | tr -d ' '
}

# ── pull ──────────────────────────────────────────────────────────
do_pull() {
    mkdir -p data

    if [ ! -f "$ENC_FILE" ]; then
        echo "state: no encrypted DB on state branch — cold start"
        return 0
    fi

    decrypt "$ENC_FILE" "$DB_PATH"

    # Verify the decrypted file is a valid SQLite database.
    if ! sqlite3 "$DB_PATH" "PRAGMA integrity_check;" >/dev/null 2>&1; then
        echo "::error::state: decrypted file fails SQLite integrity check" >&2
        rm -f "$DB_PATH"
        exit 1
    fi

    echo "state: restored $(db_size "$DB_PATH") bytes, integrity OK"
}

# ── push ──────────────────────────────────────────────────────────
do_push() {
    if [ ! -f "$DB_PATH" ]; then
        echo "state: no database to persist"
        return 0
    fi

    # Merge WAL into the main DB file so only one file needs persisting.
    sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true

    # Encrypt.
    encrypt "$DB_PATH" "$ENC_FILE"

    # Verify round-trip: decrypt to a temp file and check integrity.
    local verify_tmp
    verify_tmp="$(mktemp)"
    trap 'rm -f "$verify_tmp"' RETURN

    decrypt "$ENC_FILE" "$verify_tmp"
    if ! sqlite3 "$verify_tmp" "PRAGMA integrity_check;" >/dev/null 2>&1; then
        echo "::error::state: encrypted file fails round-trip integrity check" >&2
        exit 1
    fi

    # Commit and push (single-commit branch via --amend).
    cd "$STATE_DIR"
    git config user.name  "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add crypto_monitor.db.enc
    git commit --amend -m "state: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push --force-with-lease origin state

    echo "state: pushed $(db_size crypto_monitor.db.enc) bytes (encrypted), integrity verified"
}

# ── dispatch ──────────────────────────────────────────────────────
case "$CMD" in
    pull) do_pull ;;
    push) do_push ;;
    *)
        echo "Usage: gha_state.sh pull|push" >&2
        exit 1
        ;;
esac
