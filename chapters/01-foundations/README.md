# Chapter 1: Foundations

Before touching a model we need four tools and the ideas behind them.

## 1. Docker: packaging a process

- What a container is (a process with its own filesystem and network view,
  not a VM) and why that matters for shipping a model server with its CUDA libs.
- Image vs container, registries, `docker run`, `docker ps`.
- Hands-on: run one container, look at it from the host.

## 2. Kubernetes: running many containers on many machines

- The problem it solves: you have N machines and M workloads, who decides what
  runs where, restarts it when it dies, and routes traffic to it?
- Control plane (API server, scheduler, controller manager, etcd) vs worker
  nodes (kubelet, container runtime, kube-proxy).
- The core objects we will use constantly: Pod, Deployment, Service, Namespace.
  Everything is a declared desired state; controllers make reality match it.

## 3. kubectl: talking to the API server

- kubeconfig and contexts, `get`, `describe`, `logs`, `apply`, `port-forward`.
- Reading a manifest: apiVersion, kind, metadata, spec.

## 4. Ray: one framework for serving and training

- What Ray is (distributed Python runtime) and why it appears twice in this
  guide: Ray Serve for multi-node inference, and Ray-based RL frameworks for
  tuning later. Concept only here; hands-on when we need it.

## Hands-on: a local three-node cluster with kind

- Install: kind, kubectl.
- `kind-config.yaml`: one control plane, two workers, and why two.
- Create the cluster, inspect nodes and system pods, map each system pod to
  the component it is.
- Deploy a first Deployment and Service, reach it with port-forward.

## Checkpoint

`kubectl get nodes` shows three Ready nodes and `curl` against the
port-forwarded Service returns a response.

## What to remember for later chapters

- A GPU is just another node resource the scheduler counts.
- A model server is just a Deployment with a big image and a big memory request.
