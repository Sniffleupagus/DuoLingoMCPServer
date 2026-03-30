#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-secrets.sh — Interactive helper to create Docker Swarm secrets
#
# Usage:
#   ./setup-secrets.sh          # Create all secrets interactively
#   ./setup-secrets.sh --remove # Remove all secrets (so you can recreate)
#
# How Docker Swarm secrets work:
#   - `docker secret create <name> -` reads from stdin and stores the value
#     encrypted in Swarm's Raft log (the internal distributed database).
#   - The secret is ONLY decrypted when a service that has access to it
#     starts a container — it's mounted as /run/secrets/<name> on a tmpfs
#     (in-memory filesystem), so it never touches the container's disk.
#   - Secrets are immutable. To change one, you must remove and recreate it,
#     then update the service to pick up the new version.
#
# Prerequisites:
#   - Docker Swarm must be initialized: `docker swarm init`
#     (works fine on a single node — you don't need a cluster)
# ---------------------------------------------------------------------------

set -euo pipefail

# List of secrets this project uses.
# To adapt for another project, just change these arrays:
SECRET_NAMES=("duolingo_jwt" "duolingo_username" "duolingo_api_key")
SECRET_PROMPTS=(
    "Duolingo JWT token (from browser cookies at duolingo.com)"
    "Duolingo username"
    "Server API key (generate with: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\")"
)

# --- Handle --remove flag ---
if [[ "${1:-}" == "--remove" ]]; then
    echo "Removing existing secrets..."
    for name in "${SECRET_NAMES[@]}"; do
        if docker secret inspect "$name" &>/dev/null; then
            docker secret rm "$name"
            echo "  Removed: $name"
        else
            echo "  Not found (skipping): $name"
        fi
    done
    echo "Done. Run this script again without --remove to recreate them."
    exit 0
fi

# --- Check that Swarm is initialized ---
if ! docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null | grep -q "active"; then
    echo "Docker Swarm is not initialized. Initializing single-node swarm..."
    echo "(This is safe — it just enables the secrets feature on this machine.)"
    echo ""
    docker swarm init
    echo ""
fi

# --- Create each secret interactively ---
echo "Creating Docker Swarm secrets for DuoLingo MCP Server"
echo "======================================================"
echo ""
echo "Each value you enter will be encrypted in Docker's Raft log."
echo "It will NOT be stored as a plaintext file anywhere on disk."
echo ""

for i in "${!SECRET_NAMES[@]}"; do
    name="${SECRET_NAMES[$i]}"
    prompt="${SECRET_PROMPTS[$i]}"

    # Check if secret already exists
    if docker secret inspect "$name" &>/dev/null; then
        echo "Secret '$name' already exists. Skipping."
        echo "  (To recreate, run: ./setup-secrets.sh --remove)"
        echo ""
        continue
    fi

    echo "Creating secret: $name"
    echo "  $prompt"

    # Read the value without echoing it to the terminal (-s flag).
    # The value is piped directly to `docker secret create` — it's
    # never stored in a variable that could leak to /proc or core dumps.
    read -rsp "  Enter value: " value
    echo ""  # newline after hidden input

    if [[ -z "$value" ]]; then
        echo "  WARNING: Empty value. Skipping $name."
        echo ""
        continue
    fi

    # Pipe directly into docker secret create. The `-` means "read from stdin".
    echo -n "$value" | docker secret create "$name" -
    echo "  Created successfully."
    echo ""
done

echo "======================================================"
echo "All secrets created. You can now deploy with:"
echo ""
echo "  docker build -t duolingo-mcp:latest ."
echo "  docker stack deploy -c docker-compose.swarm.yml duolingo"
echo ""
echo "To verify secrets exist:  docker secret ls"
echo "To inspect (metadata only, value is never shown):"
echo "  docker secret inspect duolingo_jwt"
