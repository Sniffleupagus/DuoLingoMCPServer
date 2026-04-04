#!/usr/bin/env bash
# Rebuild the duolingo-mcp image and redeploy to Docker Swarm.
#
# Usage:  ./rebuild-and-deploy.sh
#
# This does NOT touch secrets — they persist in Swarm's Raft log.
# To update secrets, see docker-compose.swarm.yml header comments.

set -euo pipefail
cd "$(dirname "$0")"

echo "==> Building image..."
docker build -t duolingo-mcp:latest .

echo "==> Deploying stack to Swarm..."
docker stack deploy -c docker-compose.swarm.yml duolingo

echo "==> Waiting for service to stabilize..."
docker service ls --filter name=duolingo_duolingo-mcp

echo "==> Done. Check logs with:"
echo "    docker service logs -f duolingo_duolingo-mcp"
