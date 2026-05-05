# Nexus Hub Kubernetes Deployment Guide

This guide deploys the Nexus hub-mode topology on Kubernetes with Helm. The
runtime matches `docker-compose.hub.yml`: a Nexus RPC server, an MCP HTTP
frontend, Postgres, and Redis.

## Prerequisites

- Kubernetes cluster with a default StorageClass for the reference install.
- Helm 3.
- Access to `ghcr.io/nexi-lab/nexus`.
- An Ingress controller before exposing the MCP endpoint publicly.
- cert-manager if you want the chart to request TLS certificates through
  Ingress annotations.
- Prometheus Operator only if `podMonitor.enabled=true`.

## Reference Install

```bash
helm install nexus-hub charts/nexus-hub
kubectl rollout status deploy/nexus-hub-nexus
kubectl rollout status deploy/nexus-hub-mcp-frontend
```

The reference install deploys in-cluster Postgres and Redis. It uses the
default Postgres password `nexus`; use it only for local or evaluation
clusters.

Create the first admin token:

```bash
kubectl exec deploy/nexus-hub-nexus -- \
  nexus hub token create --name root --admin --zone root
```

The raw `sk-...` token is printed once. Save it immediately.

For local access:

```bash
kubectl port-forward svc/nexus-hub-mcp-frontend 8081:8081
curl -sf http://127.0.0.1:8081/health
```

Configure MCP clients with either header:

```text
Authorization: Bearer sk-...
X-Nexus-API-Key: sk-...
```

## Production Install

Use pinned image tags, non-default credentials, resource limits, and external
managed Postgres and Redis when possible.

Create Postgres and Redis Secrets:

```bash
kubectl create secret generic nexus-postgres \
  --from-literal=password='<postgres-password>'

kubectl create secret generic nexus-redis \
  --from-literal=redis-url='redis://redis.example.com:6379'
```

The chart builds `NEXUS_DATABASE_URL` from the Postgres values. Use a URL-safe
Postgres password, or percent-encode reserved URL characters before storing the
password in the Secret.

Example `values.prod.yaml`:

```yaml
image:
  tag: "<pinned-release>"

nexus:
  resources:
    requests:
      cpu: "1"
      memory: 2Gi
    limits:
      memory: 4Gi

mcpFrontend:
  replicaCount: 2
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
    limits:
      memory: 1Gi

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

Install:

```bash
helm install nexus-hub charts/nexus-hub -f values.prod.yaml
```

The Nexus workload uses `/healthz/startup`, `/healthz/ready`, and
`/healthz/live` for Kubernetes probes. If your cluster or image profile has
long cold-start health latency, tune `nexus.probes.*` in values instead of
editing rendered manifests.

The Nexus pod also waits for the configured Postgres host and port before
starting `nexusd`, so first boot does not race an in-cluster Postgres pod.

Keep `nexus.replicaCount` at `1` when `nexus.persistence.enabled=true`.
Scale the MCP frontend independently with `mcpFrontend.replicaCount`. To run
multiple Nexus pods for evaluation, disable Nexus persistence so each pod gets
its own `emptyDir` data directory.

## Upgrade Path

Back up Postgres before upgrades. For the in-cluster reference Postgres:

```bash
kubectl exec statefulset/nexus-hub-postgres -- \
  pg_dump -U nexus nexus > nexus-hub-backup.sql
```

For production managed Postgres, use your provider's backup workflow and verify
that a restore point exists before the Helm upgrade.

Preview changes if the `helm diff` plugin is installed:

```bash
helm diff upgrade nexus-hub charts/nexus-hub -f values.prod.yaml
```

Apply the upgrade:

```bash
helm upgrade nexus-hub charts/nexus-hub -f values.prod.yaml
kubectl rollout status deploy/nexus-hub-nexus
kubectl rollout status deploy/nexus-hub-mcp-frontend
```

Pin `image.tag` in production values. Avoid floating `latest` for upgrades
that need predictable rollback behavior.

## Operations

Check hub status:

```bash
kubectl exec deploy/nexus-hub-nexus -- nexus hub status --json
```

List tokens:

```bash
kubectl exec deploy/nexus-hub-nexus -- nexus hub token list
```

Revoke a token:

```bash
kubectl exec deploy/nexus-hub-nexus -- nexus hub token revoke <name-or-key-id>
```

Follow logs:

```bash
kubectl logs deploy/nexus-hub-nexus -f
kubectl logs deploy/nexus-hub-mcp-frontend -f
```

## Monitoring

If the cluster has the Prometheus Operator CRDs, enable:

```yaml
podMonitor:
  enabled: true
  labels:
    release: kube-prometheus-stack
```

The PodMonitor scrapes `/metrics` on the Nexus RPC pod. Leave it disabled in
clusters that do not have the `monitoring.coreos.com/v1` PodMonitor CRD.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Postgres pod is pending | No default StorageClass or insufficient storage | Set `postgres.internal.persistence.storageClass` or disable persistence for evaluation clusters |
| Nexus pod cannot connect to Postgres | Wrong external host, password, or Secret key | Check `postgres.external.*` values and the referenced Secret |
| MCP client gets 401 | Missing bearer token | Send `Authorization: Bearer sk-...` or `X-Nexus-API-Key: sk-...` |
| Ingress has no certificate | cert-manager issuer or DNS is not ready | Check Certificate, Challenge, and Ingress events |
| `helm template` fails for external Redis | Missing Redis URL | Set `redis.external.url` or `redis.external.existingSecret` |

## Uninstall

```bash
helm uninstall nexus-hub
```

PVCs are not deleted automatically by Helm in many clusters. Inspect and delete
PVCs manually only after confirming backups are complete.
