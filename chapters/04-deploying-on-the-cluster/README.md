# Chapter 4: Deploying a model on the cluster

Turn the chapter 3 `docker run` into a Kubernetes Deployment: weights from a volume, a memory limit set from measurement, health checks that mean "model loaded", and a rollout while requests are in flight.

**You will have:** vLLM serving Qwen3-0.6B behind a Service on the kind cluster, with startup, readiness and liveness probes.

**You will measure:** seconds from pod creation to first token (90 s on the test machine), and how many requests a rollout drops.

## Files

| File | What it is |
| --- | --- |
| `kind-config.yaml` | The chapter 1 cluster plus a bind mount of your Hugging Face cache into both workers |
| `manifests/vllm.yaml` | The vLLM Deployment and Service, before probes |
| `manifests/vllm-health-probes.yaml` | The same with the probes and strategy from section 6, the answer key and the starting point for chapter 5 |
| `startup_time.py` | Prints seconds from pod creation to Ready, the chapter's number |

## Prerequisites

- Chapter 3 done: the `vllm/vllm-openai-cpu:v0.29.0-arm64` image and the Qwen3-0.6B weights are on your machine.
- Docker Desktop at 10 GB. The pod alone needs 7 GiB, so close what you can on the laptop before measuring (appendix C).
- Tested on a 2021 M1 Mac with 16 GB RAM.

## 1. Create the cluster with the weights mounted

A pod cannot see your laptop's disk. kind nodes are Docker containers, so `extraMounts` in `kind-config.yaml` bind-mounts a laptop directory into each worker node, and a `hostPath` volume in the pod then reaches it. This is the laptop stand-in for a PersistentVolume; chapter 6 swaps the volume block for a PVC.

Replace `$HOME` in both `hostPath` entries with your absolute home directory first, `/Users/you` on macOS or `/home/you` on Linux. kind does not expand variables, so a forgotten edit fails at cluster creation instead of mounting nothing. Both workers get the mount because the scheduler may place the pod on either, and a `hostPath` only works on a node that has the directory.

```
kind create cluster --config chapters/04-deploying-on-the-cluster/kind-config.yaml
kind load docker-image vllm/vllm-openai-cpu:v0.29.0-arm64 --name kind-cluster
```

Each node has its own containerd image store, separate from Docker Desktop's, so `kind load` copies the 2.7 GB image into all three nodes. Without it every node would pull from Docker Hub. Verify both pieces:

```
kubectl get nodes
docker exec kind-cluster-worker ls /mnt/huggingface/hub
docker exec kind-cluster-worker crictl images | grep vllm
```

Expected: three `Ready` nodes, `models--Qwen--Qwen3-0.6B` in the listing, and the image in the node's store. `crictl` is `docker images` for the node's runtime, which is containerd, not Docker.

## 2. The Deployment, flag by flag

`manifests/vllm.yaml` is the chapter 3 command translated. Every flag has a home:

- The trailing arguments become `args`. The image's entrypoint is `vllm serve`; `args` appends to it, `command` would replace it.
- `-e` becomes `env`. `HF_HUB_OFFLINE=1` is new: vLLM otherwise contacts Hugging Face at startup even with the weights cached, and a pod's startup should not depend on an external site. `VLLM_CPU_KVCACHE_SPACE` is 1 GiB instead of chapter 3's 2: less concurrency, same single-stream speed, and a gigabyte back for the laptop (section 5).
- `-p 8000:8000` splits: `containerPort` on the pod, and a Service that gives it a stable name.
- `-v $HOME/.cache/huggingface` becomes the `hostPath` volume, node path from the kind config, mounted where vLLM looks. `type: Directory` refuses to schedule on a node without it instead of silently re-downloading.
- `--shm-size=1g` becomes an `emptyDir` with `medium: Memory` at `/dev/shm`. Kubernetes has no shm flag; a memory-backed emptyDir is a tmpfs, which is what `/dev/shm` is. Its `sizeLimit` counts against the container's memory limit.
- `resources`: the request is what the scheduler reserves, the memory limit is what the kernel enforces. Requests equal to limits puts the pod in the Guaranteed QoS class. No CPU limit: CPU limits throttle instead of kill, and throttling shows up as lower tokens per second. The 7Gi is measured, not guessed (section 5).
- `enableServiceLinks: false`. Kubernetes injects `VLLM_PORT=tcp://...` for a Service named `vllm`, and vLLM reads that as its own config and refuses to start (appendix A).

## 3. Apply, and watch a pod that is not ready say it is

```
kubectl apply -f chapters/04-deploying-on-the-cluster/manifests/vllm.yaml
kubectl get pods -w
```

In a second terminal:

```
kubectl logs -f deploy/vllm
```

The pod shows `Running 1/1` within seconds, long before the log reaches `Application startup complete`. With no probe, a container is Ready the moment its process starts, and the Service already lists it. Prove it during the startup window:

```
kubectl get endpoints vllm
kubectl port-forward svc/vllm 8000:8000
curl -s localhost:8000/v1/models
```

The endpoint is there and the curl is refused until vLLM listens. Section 6 fixes this.

## 4. Send requests and benchmark

Once startup is complete, the same curl returns the model list, and the chapter 3 benchmark works unchanged through the port-forward:

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B
```

Medians of five, next to the chapter 3 Docker number:

| where | prompt | ttft | decode |
| --- | --- | --- | --- |
| chapter 3, Docker | 44 | 158 ms | 36.9 tok/s |
| cluster, port-forward | 44 | 174 ms | 32.4 tok/s |

About 10 percent for the cluster. If you see much worse, the laptop is swapping, not the cluster (appendix C).

## 5. Measure what the pod uses

Chapter 3 ran with no memory limit, so the real number was never learned. The pod can read its own cgroup accounting:

```
kubectl exec deploy/vllm -- sh -c 'cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak; grep -E "^(anon|file) " /sys/fs/cgroup/memory.stat'
```

With the chapter 3 settings (2 GiB KV cache, no effective limit), in GiB:

| moment | current | anon | file |
| --- | --- | --- | --- |
| mid-startup, before KV cache | 5.87 | 4.16 | 1.64 |
| after benchmark | 6.66 | 5.94 | 0.68 |
| peak | 7.23 | | |

`anon` is process memory: 1.4 GiB of weights, the KV cache, and about 2.5 GiB of torch runtime across vLLM's three processes. `file` is page cache, the weights file and the compile cache, charged to the cgroup that touched it but reclaimable. The kill line is `anon`, not `current`. With the 1 GiB cache and the 7Gi limit, anon settles at 4.9 GiB and `current` sits right under the limit: the kernel fills the cgroup with page cache up to the limit by design. "Memory at the limit" is not an OOM warning; `oom_kill 0` in `memory.events` is the check.

Two log lines to notice: `Explicitly set (1.0/9.7) GiB for KV cache` and `Reducing Torch threads from 10 to 1`. vLLM sees the node's 9.7 GiB and 10 CPUs, not the pod's limit. cgroups enforce limits but do not hide the hardware, so anything that auto-sizes inside a container sizes itself wrong.

## 6. Add probes and time the startup

Three probes on `/health`, which in vLLM answers 200 only once the engine is initialized, so it means "model loaded", not "web server up". Paste this into the container in `manifests/vllm.yaml`, after `ports`:

```
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 5
            failureThreshold: 2
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
            failureThreshold: 3
```

- `startupProbe` gates the other two. Until it passes the pod is not Ready and liveness is not checked; 60 tries at 5 s is a 5 minute budget, and exhausting it restarts the container.
- `readinessProbe` controls Service membership. Two failures in a row and the pod leaves the endpoints; nothing restarts.
- `livenessProbe` restarts the container. It is the dangerous one, so it is the slowest, and it only starts after startup succeeded.

And next to `replicas`:

```
  strategy:
    type: Recreate
```

This replaces the default RollingUpdate, which would start the new pod while the old one still holds 7 GiB. Two vLLM pods do not fit on this laptop (section 7). `manifests/vllm-health-probes.yaml` is the result, if you want to diff.

```
kubectl apply -f chapters/04-deploying-on-the-cluster/manifests/vllm.yaml
kubectl get pods -w
```

Now the pod stays `Running 0/1` for the whole startup and the endpoints list stays empty until `/health` passes. The timestamps are on the pod object, so the number needs no stopwatch:

```
uv run python chapters/04-deploying-on-the-cluster/startup_time.py
```

```
vllm-689dbd6f84-bhgjm: created 07:28:51, ready 07:30:21, 90 s
```

For a second sample, `kubectl delete pod -l app=vllm` and let the ReplicaSet replace it. A benchmark run right after Ready gives a 190 ms first token, so Ready is within a second of serving.

## 7. Roll out with requests in flight

Port-forward is pinned to one pod and dies on any rollout, so the client runs inside the cluster and talks to the Service by name. It sends a short completion every few seconds and prints time, HTTP status, and duration:

```
kubectl run client --image=curlimages/curl:8.11.1 --restart=Never -- sh -c 'while true; do printf "%s " "$(date +%T)"; curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 30 -X POST http://vllm:8000/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"messages\":[{\"role\":\"user\",\"content\":\"Count to twenty.\"}],\"max_tokens\":64,\"chat_template_kwargs\":{\"enable_thinking\":false}}"; sleep 1; done'
kubectl logs -f client
```

Once the 200s are steady, in another terminal:

```
kubectl rollout restart deploy/vllm
kubectl get pods -w
```

```
07:44:15 200 0.331623s
07:44:16 000 30.003037s     <- in flight when the old pod got SIGTERM, hung to the timeout
07:44:47 000 0.002414s      <- no endpoints, 91 of these
...
07:46:18 200 4.765366s      <- new pod passed its probe
```

122 s gap, 92 failed requests: a rollout costs one full startup plus whatever was in flight. Failures are `000`, not 500, because nothing sits between the client and the pod to answer on its behalf.

For contrast, the same loop against chapter 1's whoami, which has two replicas, the default RollingUpdate, and no probe:

```
kubectl apply -f chapters/01-foundations/manifests/whoami.yaml
kubectl run client-whoami --image=curlimages/curl:8.11.1 --restart=Never -- sh -c 'while true; do printf "%s " "$(date +%T)"; curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 http://whoami; sleep 0.2; done'
kubectl logs -f client-whoami
kubectl rollout restart deploy/whoami
```

```
07:47:58 000     <- 5 s connect timeout, one request
07:48:04 200
```

One dropped request even with a spare replica: pod deletion sends SIGTERM and removes the endpoint in parallel, and kube-proxy can route one more connection to a pod that is already gone. A `preStop` sleep of a few seconds closes that gap.

What production adds to our manifest, none of which fits on this laptop: `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0` so the old pod stays until the new one is Ready; the readiness probe, which is what the rollout waits on; a `preStop` sleep; and `terminationGracePeriodSeconds` long enough for in-flight generations to finish, minutes rather than the 30 s default. Chapter 6 runs this version, where the second pod has somewhere to go.

`/health` says the engine is alive, not that it can produce output at an acceptable speed. A probe that generates one token catches a wedged engine, at the cost of a slot in every batch it runs in. Worth knowing, not worth the cost at one replica; it returns with the autoscaling signals in chapter 5.

Clean up the clients and whoami:

```
kubectl delete pod client client-whoami
kubectl delete -f chapters/01-foundations/manifests/whoami.yaml
```

## 8. Tear down

Deleting the cluster removes the pods, the Service and the image copies inside the nodes. The image in Docker Desktop and the weights in `~/.cache/huggingface` stay on the laptop, so a rebuild is section 1 again.

```
kind delete cluster --name kind-cluster
```

Keep it running if you are going straight to chapter 5, which starts from this Deployment.

## Checkpoint

```
uv run python chapters/04-deploying-on-the-cluster/startup_time.py
```

90 s from pod creation to first token on the test machine: about 40 s of torch.compile, 5 s of weights, and the rest three Python processes importing torch plus the profiling pass. Chapter 6 measures the same thing on a GPU with a 27B model, where the weights dominate.

## What to remember

- A container is Ready when its process starts, unless a probe says otherwise. Without one the Service sends traffic into a server that is still compiling.
- Set limits from measurement. The kill line is `anon` in `memory.stat`; page cache fills to the limit by design and is not an alarm.
- cgroups enforce limits but do not hide the hardware. Anything that auto-sizes inside the container sees the node, so size it explicitly.
- Port-forward is a dev tool pinned to one pod. The Service name is the endpoint, and any availability test needs a client inside the cluster.
- A rollout costs a full startup unless you can afford two pods at once, and even then the endpoint race drops a request without a preStop sleep.

## Appendix

### A. `VLLM_PORT` appears to be a URI

Without `enableServiceLinks: false`, the pod crash-loops with `ValueError: VLLM_PORT 'tcp://10.96.71.160:8000' appears to be a URI`. Kubernetes injects Docker-links style env vars for every Service in the namespace, and a Service named `vllm` produces `VLLM_PORT`, which vLLM reads as its own setting. Only Services that exist when the pod is created get injected, so it can pass on the first apply and fail on the next rollout. Alternatives: set `VLLM_PORT` explicitly in `env`, or rename the Service.

### B. OOMKilled at 6Gi

A first guess of 6Gi, from chapter 3's idle usage, crash-looped with `OOMKilled`, exit code 137, about 90 s in: right after `init engine took 46 s`, when the KV cache is allocated. `kubectl get pod -o jsonpath='{.status.containerStatuses[0].lastState}'` shows it. Idle usage is the steady state after startup, not the startup peak. Every restart also recompiled the model for 40 s, because the compile cache lives in the container filesystem that a restart discards. The fix was to raise the limit, measure (section 5), then set 7Gi.

### C. The laptop is the bottleneck, not the cluster

The first cluster benchmark gave 23.3 tok/s and 207 ms TTFT, both about 35 percent worse than Docker. Nothing competed inside the VM. On the host, `sysctl vm.swapusage` showed 6.7 GB of 8 GB swap in use: macOS was paging the Docker VM's memory to disk. Quitting the browser brought it back to 34.3 tok/s. A 10 GB VM on a 16 GB laptop leaves no room; close what you can before measuring.
