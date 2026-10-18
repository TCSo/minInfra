# Chapter 1: Foundations

Docker, Kubernetes and kubectl, learned by building a three-node cluster on your laptop, putting a load-balanced Service on it, and filling it until the scheduler says no.

**You will have:** a local Kubernetes cluster, a load-balanced Service, and a pod the scheduler refused to place, with its reason.

**You will measure:** 3 Ready nodes, a `curl` response, how many pods fit before the cluster is full (18), and how long a full rebuild from files takes (about 45 seconds).

## Files

| File | What it is |
| --- | --- |
| `kind-config.yaml` | The cluster: one control plane, two workers |
| `manifests/whoami.yaml` | A Deployment and Service that echo the pod hostname |

## Prerequisites

- Docker Desktop with its memory limit raised to 10 GB (gear icon, Resources).
- Homebrew on macOS. On Linux, install the same tools from their release pages.
- Tested on a 2021 M1 Mac with 16 GB RAM.

## 1. Install the tools

```
brew install kind kubectl
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

- kind runs Kubernetes nodes as Docker containers, so no cloud bill yet.
- kubectl is the CLI to the cluster's API server.
- uv creates the Python environment the later chapters' scripts run in.

Optional: reclaim Docker disk space first.

```
docker system df
docker builder prune
```

## 2. Create the cluster

`kind-config.yaml`, line by line:

- `kind: Cluster` is the object type. Every Kubernetes-style object has a `kind` field; the collision with the tool name is a coincidence.
- `apiVersion` is `group/version`: who owns the schema and which revision. `v1alpha4` is nominally alpha but has been unchanged for years.
- `nodes` is a list. Each entry becomes one Docker container running a full Kubernetes node. In a real cluster each would be a machine.

```
kind create cluster --config chapters/01-foundations/kind-config.yaml
kubectl wait --for=condition=Ready nodes --all
kubectl get nodes
kubectl get pods -A
docker ps
```

Expected: three nodes in `Ready`, and three `kindest/node` containers in `docker ps`. In the system pods, map each name to its component:

- Control plane, on `kind-cluster-control-plane` only: `etcd`, `kube-apiserver`, `kube-controller-manager`, `kube-scheduler`.
- Per node: `kindnet` (networking) and `kube-proxy` (Service routing).
- Cluster add-ons: `coredns` (DNS) and `local-path-provisioner` (storage).

## 3. Deploy a Service

`manifests/whoami.yaml` holds two objects separated by `---`. `kubectl apply` treats each as independent.

- `metadata.name` identifies the object. It must be unique per kind within a namespace; with none given, the `default` namespace is used.
- `spec.replicas` is the desired pod count.
- `spec.selector.matchLabels` says which pods belong to this Deployment: any pod labeled `app=whoami`.
- `spec.template` is the pod blueprint. Its `labels` must match the selector.
- `resources` is section 4.
- The Service's `selector` routes traffic to pods with those labels. Pods come and go and change IPs; the Service keeps a stable IP and DNS name, and the control plane keeps an EndpointSlice of the pods currently behind it.

Look up any field with `kubectl explain deployment.spec.template`.

```
kubectl apply -f chapters/01-foundations/manifests/whoami.yaml
kubectl get pods -o wide -w
kubectl get svc whoami
```

Wait until both pods are `Running`, then forward local port 8080 to the Service's port 80:

```
kubectl port-forward svc/whoami 8080:80
```

In another terminal:

```
for i in 1 2 3 4 5 6; do curl -s localhost:8080 | grep Hostname; done
```

Only one hostname appears. `port-forward` opens a tunnel to a single pod and stays on it, bypassing the Service's load balancing. To see both pods, send requests from inside the cluster through the Service's DNS name:

```
kubectl run curl --image=curlimages/curl --restart=Never -- sh -c 'for i in $(seq 1 20); do curl -s whoami | grep Hostname; done'
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/curl
kubectl logs curl
kubectl delete pod curl
```

## 4. Requests and the scheduler

Every whoami pod carries a `resources` block:

- `requests` is what the scheduler reserves on a node to place the pod. It is bookkeeping, not usage.
- `limits` is what the kernel enforces once the pod runs. Going over the memory limit kills the container.
- The CPU request here is deliberately large, one whole CPU for a server that uses almost none, so a few dozen replicas fill the cluster.

Each worker reports what it can offer and what is already reserved:

```
kubectl describe node kind-cluster-worker | grep -A 3 '^Allocatable'
kubectl describe node kind-cluster-worker | grep -A 4 'Allocated resources'
```

```
  cpu                1100m (11%)  0 (0%)
```

10 allocatable CPUs, the Docker VM's cores. 1.1 reserved: one whoami pod and 100m for `kindnet`. Now ask for far more than fits:

```
kubectl scale deploy/whoami --replicas=24
kubectl get pods -o wide
```

18 pods run, 9 per worker, and 6 stay `Pending`. A tenth pod would bring a worker to 10.1 CPUs. The scheduler records why in the pod's `PodScheduled` condition:

```
kubectl get pod <pending-pod> -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}{"\n"}'
```

```
0/3 nodes are available: 1 node(s) had untolerated taint(s), 2 Insufficient cpu. preemption: 0/3 nodes are available: 1 Preemption is not helpful for scheduling, 2 No preemption victims found for incoming pod.
```

One clause per node: the control plane is tainted so ordinary pods stay off it, and both workers are out of CPU. "Preemption" means evicting lower-priority pods to make room; every pod here has the same priority, so there is nothing to evict.

The workers are not busy:

```
docker stats --no-stream | grep worker
```

Each node container uses a fraction of one core. The cluster is full of reservations, not work. That is the trade requests make: a pod that has been placed is guaranteed its CPU, at the price of idle capacity nobody else can use.

Now delete a running pod:

```
kubectl delete pod <running-pod>
kubectl get deploy whoami
kubectl get pods --sort-by=.metadata.creationTimestamp
```

The Deployment still says 24. Its controller saw 23, created a replacement, and the freed CPU went to whichever pending pod the scheduler picked, often an older one; the new replacement is the one that waits. You declared 24 and the controller keeps making it true. Scale back:

```
kubectl scale deploy/whoami --replicas=2
```

## 5. Rebuild from files and time it

Delete everything and recreate it from the checked-in files. Time it.

```
kind delete cluster --name kind-cluster
kind create cluster --config chapters/01-foundations/kind-config.yaml
kubectl wait --for=condition=Ready nodes --all
kubectl apply -f chapters/01-foundations/manifests/whoami.yaml
kubectl rollout status deploy/whoami
```

Then repeat the in-cluster `curl` from section 3. Reference: about 45 seconds on the test machine, with images already pulled. Tear down when done:

```
kind delete cluster --name kind-cluster
```

## Checkpoint

```
kubectl get nodes
```

Three nodes `Ready`, the in-cluster `curl` returns two different `Hostname`s, and a scaled-up whoami leaves pods `Pending` with `Insufficient cpu`.

## What to remember

- Everything is declared desired state. A controller makes reality match it: delete a pod and the Deployment controller replaces it.
- `kubectl port-forward` pins to one pod. Load balancing happens inside the cluster.
- `kubectl explain <kind>.<path>` is the schema for the exact version you run.
- The scheduler places pods by `requests`, not by what they use. A full cluster can be an idle one.
- The `PodScheduled` condition on a `Pending` pod says exactly why, one clause per node.
- Cluster state survives a Docker restart. Port-forwards do not.
- Delete and recreate from files is the test that the chapter is done.
- A GPU is just another node resource the scheduler counts, and a model server is just a Deployment with a big image and a big memory request. Chapter 2 works out how big.

## Appendix

### A. What you built, and how it maps to production

Bottom to top: your Mac, Docker Desktop's Linux VM, three `kindest/node` containers acting as Kubernetes nodes, the Kubernetes system pods, and your whoami pods.

| Layer | What you have | What production has | Chapter |
| --- | --- | --- | --- |
| Machines | 3 Docker containers on one Mac | VMs or bare metal, some with GPUs | 5 |
| Cluster | kind | EKS, GKE, AKS or self-managed | 5 |
| Networking | port-forward | LoadBalancer, Ingress, an L7 router | 7 |
| Storage | local-path-provisioner | Persistent volumes, object storage for weights | 4 |
| Workload | whoami | vLLM serving Qwen3-8B | 3, 4 |
| Scaling | you edit `replicas` | a metric edits it for you, and a node autoscaler adds machines | 6 |
| Observability | `kubectl get`, `describe`, events | Prometheus, Grafana, alerts | 5, 6 |
| Access | you, on localhost | your coding agent, with auth | 8 |

Every row on the left is the same Kubernetes object as the right. The rest of the guide swaps rows one at a time.
