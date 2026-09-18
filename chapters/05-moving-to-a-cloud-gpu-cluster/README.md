# Chapter 5: Moving to a cloud GPU cluster

The chapter 4 pod serves one user well. This chapter gives it sixteen, tries to add a second pod, then moves the same manifests to a rented GPU and runs Qwen3.8-27B on it.

**You will have:** the laptop's ceiling under concurrent load, a priced comparison of four providers, and the chapter 4 Deployment serving Qwen3.8-27B-FP8 on a single cloud GPU.

**You will measure:** seconds to write a 1024-token program at 1 vs 8 concurrent users (39 s vs 110 s on the test machine), tokens per second at 1 and 16 streams on the GPU, and dollars per million tokens.

## Files

The laptop sections reuse earlier chapters' files. The cloud sections add manifests here.

| File                                                               | What it is                                                                             |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| `../03-serving-with-vllm/benchmark.py`                             | The chapter 3 benchmark, now with `--concurrency N` and a fixed program-writing prompt |
| `../04-deploying-on-the-cluster/kind-config.yaml`                  | The laptop cluster with the weights mounted                                            |
| `../04-deploying-on-the-cluster/manifests/vllm-health-probes.yaml` | The Deployment under test in sections 1 and 2, and the base for the cloud manifest     |

## Prerequisites

- Chapter 4 done. The cluster can be gone; section 1 rebuilds it in two minutes.
- Docker Desktop at 10 GB, and the browser closed before timed runs (chapter 4, appendix C).
- For section 4 onward: a Nebius account with the $25 minimum deposit. The cloud sections cost under $30 if you tear down at the end.
- Tested on a 2021 M1 Mac with 16 GB RAM.

## 1. More than one user

The benchmark gets two flags. `--concurrency N` fires N requests at once and reports the pod's aggregate tokens per second next to the per-stream medians. `--program` uses a fixed "write a complete Python program" prompt. `ignore_eos` is already set, so every request generates exactly `--max-tokens` tokens and the rows are comparable.

Rebuild the chapter 4 cluster if it is gone. kind does not expand `$HOME`, so substitute it into a copy of the config:

```
sed "s|\$HOME|$HOME|g" chapters/04-deploying-on-the-cluster/kind-config.yaml > /tmp/kind-config.yaml
kind create cluster --config /tmp/kind-config.yaml
kind load docker-image vllm/vllm-openai-cpu:v0.29.0-arm64 --name kind-cluster
kubectl apply -f chapters/04-deploying-on-the-cluster/manifests/vllm-health-probes.yaml
kubectl rollout status deploy/vllm --timeout=10m
kubectl port-forward svc/vllm 8000:8000
```

In a second terminal, watch the scheduler's queue and cache:

```
while true; do
  curl -s localhost:8000/metrics \
    | grep -E '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|num_preemptions_total)\{' \
    | sed -E 's/\{[^}]*\}//' | tr '\n' ' '
  echo
  sleep 2
done
```

In a third, the sweep. About 12 minutes:

```
for n in 1 2 4 8 16; do
  uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-0.6B --program --max-tokens 1024 --runs 1 --concurrency $n
done
```

Reference run. The N=1 row is the single-stream output; its aggregate is 1024 tokens over the printed total.

| N   | aggregate tok/s | median TTFT | per-stream decode | median total | wall    |
| --- | --------------- | ----------- | ----------------- | ------------ | ------- |
| 1   | 26.3            | 288 ms      | 26.5 tok/s        | 38.9 s       | 38.9 s  |
| 2   | 51.2            | 436 ms      | 25.9 tok/s        | 40.0 s       | 40.0 s  |
| 4   | 67.6            | 761 ms      | 17.1 tok/s        | 60.6 s       | 60.6 s  |
| 8   | 74.3            | 1487 ms     | 9.4 tok/s         | 110.3 s      | 110.3 s |
| 16  | 76.0            | 2444 ms     | 7.2 tok/s         | 145.2 s      | 215.5 s |

Two ceilings, different resources.

**Compute.** The second user is free: aggregate doubles and nobody slows down, because decode reads the weights once per step regardless of batch size. By N=8 the aggregate stops at about 75 tok/s. The pod has 4 cores, and the batched matmul is now compute-bound. From here everyone shares 75 tok/s: a program that took 39 s alone takes 110 s with seven neighbours.

**Memory.** Rows 1 to 8 have wall equal to median total, so every stream ran the whole time. Row 16 does not: the median stream finished at 145 s, the slowest at 215 s. The metrics loop shows why, one line every 2 s:

```
running 16.0 waiting 0.0 kv_cache_usage_perc 0.889 preemptions 16.0
running 14.0 waiting 2.0 kv_cache_usage_perc 0.972 preemptions 16.0   <- cache full, first 2 evicted
running 12.0 waiting 4.0 kv_cache_usage_perc 1.000 preemptions 16.0
running  8.0 waiting 8.0 kv_cache_usage_perc 1.000 preemptions 16.0   <- 8 survive, 8 evicted
running  1.0 waiting 7.0 kv_cache_usage_perc 0.111 preemptions 17.0   <- survivors done at ~145 s
running  4.0 waiting 4.0 kv_cache_usage_perc 0.375 preemptions 19.0   <- evicted readmitted, re-prefilled
running  8.0 waiting 0.0 kv_cache_usage_perc 0.736 preemptions 24.0
running  0.0 waiting 0.0 kv_cache_usage_perc 0.000 preemptions 24.0   <- ~215 s
```

Every decode step adds one token per stream to the KV cache. When it is full, vLLM evicts whole requests so the rest can continue. An evicted request goes back to the waiting queue, and when readmitted its prompt plus everything it had generated is prefilled again. Eight survived, eight were evicted, and the second wave is the 70 s gap.

The arithmetic: usage moves in steps of 1/72, so the 1 GiB cache is 72 blocks of 128 tokens, 9,216 tokens. One request of 70 prompt plus 1024 generated tokens needs 9 blocks. Eight requests need 72, so N=8 fits with nothing to spare and N=16 needs twice the cache that exists. The preemption counter ticks at readmission rather than eviction (appendix A).

A GPU raises both ceilings at once: an order of magnitude more bandwidth and compute, and tens of gigabytes of cache instead of one. Before renting one, the obvious fix.

## 2. Try to scale

Cordon `kind-cluster-worker2` first. Both kind workers share one 9.7 GiB Docker VM, and without the cordon the second pod lands on worker2 and takes the VM down.

```
kubectl cordon kind-cluster-worker2
kubectl scale deploy/vllm --replicas=2
kubectl get pods
```

The new pod stays `Pending`. The scheduler's reasoning is in the pod's `PodScheduled` condition:

```
kubectl get pod <pending-pod> -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}{"\n"}'
```

```
0/3 nodes are available: 1 Insufficient memory, 1 node(s) had untolerated taint(s), 1 node(s) were unschedulable. preemption: 0/3 nodes are available: 1 No preemption victims found for incoming pod, 2 Preemption is not helpful for scheduling.
```

One clause per node: the control plane is tainted, worker2 is cordoned, worker1 has insufficient memory. "Preemption" here is Kubernetes evicting lower-priority pods, unrelated to vLLM's cache eviction in section 1; every pod has the same priority, so there is nothing to evict.

Why memory and not CPU:

```
kubectl describe node kind-cluster-worker | grep -A 8 'Allocated resources'
```

```
  Resource           Requests      Limits
  cpu                4100m (41%)   0 (0%)
  memory             7218Mi (72%)  7Gi (72%)
```

The node has 10 allocatable CPUs and about 9.8 GiB of memory. A second 4-CPU request fits; a second 7 GiB request does not.

Had it fit, it would not have helped: both workers are the same VM with the same 10 cores, so two replicas split the 75 tok/s, not double it. Scaling out means another machine, and after section 1, one with an accelerator. The laptop cluster is done for this chapter:

```
kind delete cluster --name kind-cluster
```

## 3. Pick a provider

What the rest of the course needs from one:

1. Self-serve: a credit card and no sales call.
2. Managed Kubernetes, so the chapter 4 manifests apply as they are.
3. A GPU node pool that scales to zero, so the endpoint costs nothing while idle (chapter 6).
4. A single GPU that holds Qwen3.8-27B-FP8 (30.9 GB) with room for KV cache, per the chapter 2 fit table.
5. Cheap per GPU-hour, and cheap per month at idle.

Prices checked 2026-09-16. They move monthly; re-check on the day and write the date next to the number.

|                                             | Nebius                                                                                | DigitalOcean                 | GKE                                                                              | EKS                                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Card that fits, region                      | RTX PRO 6000 96 GB, us-central1 (Kansas City); L40S 48 GB only in eu-north1 (Finland) | L40S 48 GB, US regions       | A100 80 GB, us-central1 / us-east4 / us-east5 only                               | L40S 48 GB (g6e.xlarge), most regions                                |
| On-demand $/GPU-hour                        | 1.80                                                                                  | 1.57                         | 5.07                                                                             | 1.86                                                                 |
| Spot / preemptible $/GPU-hour               | 0.95                                                                                  | offered, price not published | 1.79 to 2.93 by region                                                           | 1.38                                                                 |
| Control plane while the GPU pool is at zero | free                                                                                  | free ($40/month for HA)      | free for one zonal cluster                                                       | $0.10/hour, about $73/month, as long as the cluster exists           |
| GPU node pool scales to zero                | yes: docs imply it, third parties run it, this chapter checks it                      | yes, documented              | yes, documented                                                                  | yes (managed node group min 0, or Karpenter)                         |
| Getting in                                  | credit card, $25 deposit, no quota                                                    | credit card, L40S self-serve | GPU quota request, often refused for new accounts; trial credits cannot buy GPUs | existing account plus a G-instance vCPU quota request, usually a day |
| 2-GPU NVLink shape (chapter 10)             | none; 8x H200 node for an hour                                                        | none; 8x H100 node           | a2-highgpu-2g                                                                    | none under 8 GPUs                                                    |

This course uses Nebius, RTX PRO 6000, us-central1: the most memory and bandwidth per dollar in the chart, in a US region, with no quota step. In chapter 2's tables the card has a 52 tok/s single-stream ceiling on the FP8 checkpoint and 55.5 GB of KV cache after the weights, about 850,000 tokens, against 9,216 in section 1.

If you pick differently, the manifests still apply. Know before you start: GKE needs a GPU quota request that new accounts often lose, EKS bills the control plane every hour the cluster exists even at zero GPU nodes, and DigitalOcean's L40S has half the memory and bandwidth.

Two more facts for the day you rent. Availability changes daily: on 2026-09-17 the Nebius console offered the RTX PRO 6000 in us-central1 only as a preemptible node, at $0.98 per hour, with no on-demand shape to buy. A preemptible VM can be stopped by the provider at any time, which is fine for this chapter, and the cost checkpoint prints dollars per million tokens at both prices. And latency is not a reason to pick a region: a 50 ms round trip is paid once per request before the first token, and decode tokens per second never sees the network.

Sections 4 onward create the cluster, download the weights once onto a persistent disk, run the chapter 4 manifest on the GPU, measure, and tear everything down. They are being written.

## Appendix

### A. The preemption counter lags

During the N=16 run, `vllm:num_preemptions_total` stayed flat while eight requests were evicted, then climbed by eight during readmission. vLLM records the preemption as an event on the request, and events reach the metrics logger with the request's next output token, which an evicted request does not produce until it is back in the batch. Read the counter after the run; use `kv_cache_usage_perc` and `num_requests_waiting` for the live view.
