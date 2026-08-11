# Health Service

## Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) only if
you want to run the Python development workflow or tests locally. It downloads
and manages the project-pinned Python 3.13.15 interpreter.

The local Kubernetes workflow requires:

- [Docker](https://docs.docker.com/engine/install/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm 3](https://helm.sh/docs/intro/install/)

## Python development setup (optional)

```bash
uv sync --locked
```

`uv.lock` is committed to the repository. The `--locked` option installs only
the exact dependency versions recorded there.

## Local deployment with kind and Helm

The Helm chart is the Kubernetes source of truth; no separate raw manifest set
is maintained. It deploys one application `Deployment`, one PostgreSQL
`StatefulSet`, their internal Services, a Secret, and one persistent volume
claim. PostgreSQL starts automatically with the application, and the service
creates its database tables automatically after startup.

### 1. Build the image, create the cluster, and load the image

```bash
docker build --tag health-service:0.1.0 .
kind create cluster --name health-service
kind load docker-image health-service:0.1.0 --name health-service
```

Loading the locally built image avoids needing a registry. The chart uses
`IfNotPresent`, so Kubernetes uses the image loaded into the kind node.

### 2. Install the chart

```bash
helm upgrade --install health-service charts/health-service \
  --namespace health-service \
  --create-namespace \
  --wait \
  --timeout 3m
```

The default database password is intentionally only a local demo credential.
Override it on the first install when needed:

```bash
helm upgrade --install health-service charts/health-service \
  --namespace health-service \
  --create-namespace \
  --set-string postgresql.auth.password=a-local-password \
  --wait
```

PostgreSQL initialization variables only apply to an empty data directory. Do
not change this value later while retaining the existing PVC unless the
database password is also migrated.

### 3. Inspect and access the service

```bash
kubectl --namespace health-service get pods,services,pvc
kubectl --namespace health-service logs deployment/health-service
kubectl --namespace health-service logs statefulset/health-service-postgresql
helm test health-service --namespace health-service --logs
```

Forward the ClusterIP Service in a dedicated terminal:

```bash
kubectl --namespace health-service \
  port-forward service/health-service 8000:8000
```

See the [API usage guide](docs/apiusage.md) for all supported endpoints with
sample requests and responses.

See the [MVP architecture document](docs/architecture.md) for the datastore
choice, component design, operational behavior, and architectural tradeoffs.

### 4. Clean up

```bash
helm uninstall health-service --namespace health-service
kubectl --namespace health-service delete pvc data-health-service-postgresql-0
kind delete cluster --name health-service
```

## Test
For running all unit tests and integration tests
```bash
uv run pytest
```

Run only the integration test with:

```bash
uv run pytest -m integration
```
