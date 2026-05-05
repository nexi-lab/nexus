# nexus-hub Helm Chart

This chart deploys the Nexus hub-mode topology from `docker-compose.hub.yml`:

- Nexus RPC server on port 2026 with gRPC on 2028
- MCP HTTP frontend on port 8081
- Postgres for auth, zones, metadata, and hub tokens
- Redis for audit publish, rate limits, and hub metrics

## Reference install

```bash
helm install nexus-hub charts/nexus-hub
kubectl rollout status deploy/nexus-hub-nexus
kubectl rollout status deploy/nexus-hub-mcp-frontend
kubectl exec deploy/nexus-hub-nexus -- \
  nexus hub token create --name root --admin --zone root
kubectl port-forward svc/nexus-hub-mcp-frontend 8081:8081
```

The reference install uses the default Postgres password `nexus`. Override it
before first production install.

## Production values

```yaml
image:
  tag: "<pinned-release>"

postgres:
  internal:
    enabled: false
  external:
    host: postgres.example.com
    port: 5432
    database: nexus
    username: nexus
    existingSecret: nexus-postgres
    existingSecretPasswordKey: password

redis:
  internal:
    enabled: false
  external:
    existingSecret: nexus-redis
    existingSecretUrlKey: redis-url

ingress:
  enabled: true
  className: nginx
  host: nexus.example.com
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  tls:
    enabled: true
    secretName: nexus-hub-tls
```

## Important values

| Value | Default | Purpose |
| --- | --- | --- |
| `image.repository` | `ghcr.io/nexi-lab/nexus` | Nexus image repository |
| `image.tag` | `latest` | Nexus image tag |
| `nexus.profile` | `full` | Runtime profile passed to `nexusd` |
| `nexus.replicaCount` | `1` | Nexus RPC replicas |
| `mcpFrontend.replicaCount` | `1` | MCP frontend replicas |
| `postgres.internal.enabled` | `true` | Deploy in-cluster Postgres |
| `redis.internal.enabled` | `true` | Deploy in-cluster Redis |
| `ingress.enabled` | `false` | Create Ingress for MCP frontend |
| `podMonitor.enabled` | `false` | Create a Prometheus Operator PodMonitor |
