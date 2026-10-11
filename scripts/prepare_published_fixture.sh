#!/usr/bin/env bash
set -euo pipefail

node="$1"
client="$2"
destination="$3"
deadline=$((SECONDS + 120))
until docker logs "$node" 2>&1 | grep -F 'VFS data plane ready' >/dev/null; do
  if [ "$(docker inspect "$node" --format '{{.State.Running}}')" != true ] || [ "$SECONDS" -ge "$deadline" ]; then
    docker logs --tail=100 "$node"
    exit 1
  fi
  sleep 1
done

docker exec "$node" sh -c \
  'nexusd-cluster auth mint --subject-type user --subject-id operator --admin > /tmp/search-admin-key'
docker exec -u 0 "$client" mkdir -p "$destination/tls" "$destination/agent-a" "$destination/agent-b"
docker cp "$node:/app/data/tls/." - | docker cp - "$client:$destination/tls"
docker cp "$node:/tmp/search-admin-key" - | docker cp - "$client:$destination"
for label in a b; do
  bundle=$(docker exec "$node" nexusd-cluster auth mint --subject-type agent --subject-id "search-cert-${label}")
  docker cp "$node:$bundle/." - | docker cp - "$client:$destination/agent-${label}"
done
docker exec -u 0 "$client" chown -R nexus:nexus "$destination"
