# Chapter 5: Moving to a cloud GPU cluster

The chapter 4 pod serves one user well. This chapter gives it sixteen, tries to add a second pod, then moves the same manifests to a rented GPU, runs Qwen3-8B-FP8 on it, measures, and deletes the cluster.

**You will have:** the laptop's ceiling under concurrent load, a priced comparison of four providers, the chapter 4 Deployment serving Qwen3-8B-FP8 on a single cloud GPU, and a cluster that exists only while you use it.

**You will measure:** seconds to write a 1024-token program at 1 vs 8 concurrent users (39 s vs 110 s on the test machine); on the GPU, minutes from nothing to first token (14 min cluster, 290 s pod), then TTFT, time per output token, throughput, goodput and dollars per million tokens from 1 to 128 concurrent users ($6.35 at one user, $0.25 at 32 within the latency target).

## Files

The laptop sections reuse earlier chapters' files. The cloud sections add manifests here.

| File                                                               | What it is                                                                             |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| `../03-serving-with-vllm/benchmark.py`                             | The chapter 3 benchmark, now with `--concurrency N` and a fixed program-writing prompt |
| `../04-deploying-on-the-cluster/kind-config.yaml`                  | The laptop cluster with the weights mounted                                            |
| `../04-deploying-on-the-cluster/manifests/vllm-health-probes.yaml` | The Deployment under test in sections 1 and 2, and the base for the cloud manifest     |
| `../04-deploying-on-the-cluster/startup_time.py`                   | Seconds from pod creation to Ready, reused on the GPU                                  |
| `eks-cluster.yaml`                                                 | The EKS cluster: one managed node group of one g5.2xlarge, across every zone that sells it |
| `manifests/vllm-gpu.yaml`                                          | The chapter 4 Deployment and Service, changed for one GPU and Qwen3-8B-FP8             |

## Prerequisites

- Chapter 4 done. The cluster can be gone; section 1 rebuilds it in two minutes.
- Docker Desktop at 10 GB, and the browser closed before timed runs (chapter 4, appendix C).
- For section 4 onward: an AWS account with the AWS CLI logged in, `eksctl`, and a G-instance vCPU quota of at least 8 in your region (section 4 shows how to check and request it; new accounts start at 0 and the request can take a day). The cloud sections cost under $10 if you tear down at the end.
- Tested on a 2021 M1 Mac with 16 GB RAM.

## 1. More than one user

The benchmark gets two flags. `--concurrency N` fires N requests at once and reports the pod's aggregate tokens per second next to the per-stream medians. `--program` uses a fixed "write a complete Python program" prompt. `ignore_eos` is already set, so every request generates exactly `--max-tokens` tokens and the rows are comparable.

Above N=1 each line reports time per output token (TPOT) instead of decode speed; 1000 divided by TPOT in ms is the per-stream tokens per second in the table below. It also reports goodput, the tokens from requests that met a 1 s TTFT and 50 ms TPOT target. The GPU sections use goodput; on the laptop it drops to zero by N=4.

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

Cordon the worker that is not running vLLM first. Both kind workers share one 9.7 GiB Docker VM, and without the cordon the second pod lands on the free worker and takes the VM down.

```
VLLM_NODE=$(kubectl get pod -l app=vllm -o jsonpath='{.items[0].spec.nodeName}')
kubectl get nodes -o name | grep worker | grep -v "$VLLM_NODE\$" | xargs kubectl cordon
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

One clause per node: the control plane is tainted, the free worker is cordoned, the vLLM worker has insufficient memory. "Preemption" here is Kubernetes evicting lower-priority pods, unrelated to vLLM's cache eviction in section 1; every pod has the same priority, so there is nothing to evict.

Why memory and not CPU:

```
kubectl describe node $VLLM_NODE | grep -A 8 'Allocated resources'
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
4. A GPU shape you can actually get on the day: one that holds the working model, Qwen3-8B-FP8 (9.4 GB), with room for KV cache, and a 48 GB or larger shape for the headline rerun on Qwen3.8-27B-FP8 (30.9 GB), per the chapter 2 fit tables.
5. Cheap per GPU-hour, and cheap per month at idle.

Prices checked 2026-09-16, the AWS column again on 2026-10-13. They move monthly; re-check on the day and write the date next to the number.

|                                             | Nebius                                                                                | DigitalOcean                 | GKE                                                                              | EKS                                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Card that fits, region                      | RTX PRO 6000 96 GB, us-central1 (Kansas City); L40S 48 GB only in eu-north1 (Finland) | L40S 48 GB, US regions       | A100 80 GB, us-central1 / us-east4 / us-east5 only                               | A10G 24 GB (g5.2xlarge), L40S 48 GB (g6e.2xlarge), RTX PRO 6000 96 GB (g7e.2xlarge); all US regions |
| On-demand $/GPU-hour                        | 1.80                                                                                  | 1.57                         | 5.07                                                                             | 1.21 / 2.24 / 3.36                                                   |
| Spot / preemptible $/GPU-hour               | 0.95                                                                                  | offered, price not published | 1.79 to 2.93 by region                                                           | g7e.2xlarge 1.63 to 3.36 by zone on 2026-10-13                        |
| Control plane while the GPU pool is at zero | free                                                                                  | free ($40/month for HA)      | free for one zonal cluster                                                       | $0.10/hour, about $73/month, as long as the cluster exists           |
| GPU node pool scales to zero                | yes: docs imply it, third parties run it                                              | yes, documented              | yes, documented                                                                  | yes (managed node group min 0, or Karpenter; chapter 6)              |
| Getting in                                  | credit card, $25 deposit, no quota                                                    | credit card, L40S self-serve | GPU quota request, often refused for new accounts; trial credits cannot buy GPUs | existing account plus a G-instance vCPU quota request, usually a day |
| 2-GPU NVLink shape (chapter 10)             | none; 8x H200 node for an hour                                                        | none; 8x H100 node           | a2-highgpu-2g                                                                    | none under 8 GPUs                                                    |

On paper the pick is Nebius's RTX PRO 6000, the most memory and bandwidth per dollar in the chart. In September and October 2026 it was out of stock there, and the same card and the L40S were out of stock in every zone of AWS us-west-2. Appendix B shows what that looks like.

This course uses the card every cloud keeps in stock: AWS EKS and the g5.2xlarge, one A10G with 24 GB, $1.21 per hour on-demand in us-west-2. It holds the working model with about 136,000 tokens of FP8 KV cache against 9,216 in section 1 (section 5), and a 64 tok/s single-stream ceiling from chapter 2. The 27B needs the 48 GB L40S or the 96 GB RTX PRO 6000; the same config with `g6e.2xlarge` or `g7e.2xlarge` on the instance line is the headline rerun for the day they are in stock, and chapter 2's tables say what to expect from each.

If you pick differently, the manifests still apply. Know before you start: GKE needs a GPU quota request that new accounts often lose, EKS bills the control plane every hour the cluster exists even at zero GPU nodes, and every provider's newest card is the one most likely to be out of stock. Two more facts for the day you rent. Never pin a GPU node group to one zone; give it every zone in the region and let the provider find capacity. And latency is not a reason to pick a region: a 50 ms round trip is paid once per request before the first token, and decode tokens per second never sees the network.

## 4. Create the cluster

Two checks before anything is billed. The first is your quota for G-family instances, counted in vCPUs. A g5.2xlarge has 8:

```
aws service-quotas get-service-quota --region us-west-2 --service-code ec2 --quota-code L-DB2E81BA --query Quota.Value
```

```
8.0
```

If it prints 0, request 8 and wait for the email, usually within a day:

```
aws service-quotas request-service-quota-increase --region us-west-2 --service-code ec2 --quota-code L-DB2E81BA --desired-value 8
```

The second is which zones sell the card:

```
aws ec2 describe-instance-type-offerings --region us-west-2 --location-type availability-zone \
  --filters Name=instance-type,Values=g5.2xlarge --query 'InstanceTypeOfferings[].Location' --output text
```

```
us-west-2b	us-west-2c	us-west-2a
```

us-west-2 has a fourth zone, 2d, that does not offer the g5. `eks-cluster.yaml` lists the other three:

```yaml
availabilityZones: [us-west-2a, us-west-2b, us-west-2c]

managedNodeGroups:
  - name: gpu
    instanceType: g5.2xlarge
    amiFamily: AmazonLinux2023
    minSize: 0
    maxSize: 1
    desiredCapacity: 1
    volumeSize: 100
    volumeType: gp3
    volumeIOPS: 4000
    volumeThroughput: 1000
```

- `availabilityZones`: eksctl creates a subnet in each, and the node group may launch in any of them. The cloud picks whichever zone has a machine.
- `instanceType` and `amiFamily`: for a GPU instance type, eksctl picks the EKS accelerated image, which ships the NVIDIA driver and container toolkit.
- `minSize: 0`, `desiredCapacity: 1`: one node now; chapter 6 scales it to zero.
- `volume*`: the node's disk holds the 8.7 GB vLLM image and the 8.8 GiB of weights. The gp3 default caps both at 125 MB/s; 1000 MB/s is the gp3 maximum.

Billing starts with this command: $0.10/hour for the control plane, $1.21/hour once the node runs.

```
time eksctl create cluster -f chapters/05-moving-to-a-cloud-gpu-cluster/eks-cluster.yaml
```

The lines that matter, from the reference run:

```
22:47:29 [ℹ]  creating EKS cluster "mininfra" in "us-west-2" region with managed nodes
22:47:30 [ℹ]  deploying stack "eksctl-mininfra-cluster"
22:57:31 [ℹ]  creating addon: vpc-cni
22:59:34 [ℹ]  deploying stack "eksctl-mininfra-nodegroup-gpu"
23:01:46 [✔]  saved kubeconfig as "/Users/you/.kube/config"
23:01:47 [ℹ]  as you are using the EKS-Optimized Accelerated AMI with a GPU-enabled instance type, the Nvidia Kubernetes device plugin was automatically installed.
23:01:48 [✔]  EKS cluster "mininfra" in "us-west-2" region is ready
eksctl create cluster -f   0.77s user 0.46s system 0% cpu 14:20.11 total
```

| Step                                   | Time         |
| -------------------------------------- | ------------ |
| Control plane                          | 10 min       |
| Add-ons (networking, DNS, metrics)     | 2 min        |
| GPU node from launch to joined         | 2 min 10 s   |
| Total                                  | 14 min 20 s  |

The control plane is most of it. Scaling the node group to zero and back (chapter 6) repeats only the GPU node row. Two warnings in the output can be ignored today: Auto Mode becoming eksctl's default in a future release, and the vpc-cni add-on having no IAM role (no pod here calls AWS).

eksctl has written the kubeconfig, so `kubectl` now talks to EKS:

```
kubectl get nodes -o custom-columns='NAME:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,GPU:.status.allocatable.nvidia\.com/gpu,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'
```

```
NAME                                           ZONE         TYPE         GPU   CPU     MEM
ip-192-168-14-127.us-west-2.compute.internal   us-west-2a   g5.2xlarge   1     7910m   31465412Ki
```

`GPU 1` is the NVIDIA device plugin, a DaemonSet eksctl installed, advertising the card to the scheduler as a resource named `nvidia.com/gpu`. Without it the node has a GPU and Kubernetes cannot schedule onto it.

## 5. The chapter 4 Deployment on a GPU

`manifests/vllm-gpu.yaml` is the chapter 4 health-probe manifest with these changes:

```
diff chapters/04-deploying-on-the-cluster/manifests/vllm-health-probes.yaml chapters/05-moving-to-a-cloud-gpu-cluster/manifests/vllm-gpu.yaml
```

```
<           image: vllm/vllm-openai-cpu:v0.29.0-arm64
>           image: vllm/vllm-openai:v0.29.0
<             - Qwen/Qwen3-0.6B
<             - --dtype=float16
<             - --max-model-len=4096
>             - Qwen/Qwen3-8B-FP8
>             - --max-model-len=32768
>             - --kv-cache-dtype=fp8
<           env:
<             - name: VLLM_CPU_KVCACHE_SPACE
<               value: "1"
<             - name: HF_HUB_OFFLINE
<               value: "1"
<             periodSeconds: 5
<             failureThreshold: 60
>             periodSeconds: 10
>             failureThreshold: 90
<               memory: 7Gi
>               memory: 16Gi
<               memory: 7Gi
>               nvidia.com/gpu: "1"
>               memory: 24Gi
<             type: Directory
>             type: DirectoryOrCreate
```

- The image is the CUDA build of the same vLLM version.
- `--kv-cache-dtype=fp8` stores the cache in one byte per value instead of two, doubling the tokens that fit.
- The CPU cache size and offline mode go: the GPU cache is sized automatically, and the weights now download from Hugging Face on first start.
- The startup probe allows 90 × 10 s = 15 minutes, enough for the image pull and download on a new node.
- `nvidia.com/gpu: "1"` under limits asks the scheduler for the card the device plugin advertised. The memory figures are host RAM, not GPU memory.
- `DirectoryOrCreate`: the hostPath at `/mnt/huggingface` is empty on a new node, and the weights land there for the next pod on it.

In one terminal, watch the pod's events:

```
kubectl get events --sort-by=.lastTimestamp -w
```

In another, apply and follow the logs. The first `logs` fails while the image is still pulling; rerun it after the `Started` event.

```
kubectl apply -f chapters/05-moving-to-a-cloud-gpu-cluster/manifests/vllm-gpu.yaml
kubectl logs -f deploy/vllm
```

The events:

```
Normal    Pulling     Pulling image "vllm/vllm-openai:v0.29.0"
Normal    Pulled      Successfully pulled image "vllm/vllm-openai:v0.29.0" in 1m53.306s (1m53.306s including waiting). Image size: 8674228795 bytes.
Normal    Started     Started container vllm
Warning   Unhealthy   Startup probe failed: Get "http://192.168.8.134:8000/health": dial tcp 192.168.8.134:8000: connect: connection refused
```

The probe failures repeat until the server opens its port; that is the startup probe doing its job. The log lines that matter:

```
[weight_utils.py:526] Time spent downloading weights for Qwen/Qwen3-8B-FP8: 20.178136 seconds
[default_loader.py:430] Loading weights took 20.82 seconds
[marlin_utils_fp8.py:112] Your GPU does not have native support for FP8 computation but FP8 quantization is being used. Weight-only FP8 compression will be used leveraging the Marlin kernel. This may degrade performance for compute-heavy workloads.
[model_runner.py:404] Model loading took 8.9 GiB memory and 43.199166 seconds
[gpu_worker.py:625] Available KV cache memory: 9.36 GiB
[kv_cache_utils.py:2032] GPU KV cache size: 136,272 tokens, Maximum concurrency for 32,768 tokens per request: 4.16x
[model_runner.py:960] Graph capturing finished in 8 secs, took 0.56 GiB
[gpu_worker.py:860] Free memory on device (22.05/22.3 GiB) on startup. Desired GPU memory utilization is (0.92, 20.52 GiB). Actual usage is 9.17 GiB for consumed memory (weights + non-torch), 1.99 GiB for peak activation, and 0.56 GiB for CUDAGraph memory. [...] Current kv cache memory in use is 9.36 GiB.
[core.py:361] init engine (profile, create kv cache, warmup model) took 67.08 s (compilation: 43.83 s)
INFO:     Application startup complete.
```

When the pod is Ready:

```
uv run python chapters/04-deploying-on-the-cluster/startup_time.py
```

```
vllm-64546cbb55-xwfb7: created 07:08:18, ready 07:13:08, 290 s
```

Chapter 4's laptop took 90 s with the image preloaded and the 0.6B on disk. The 290 s here:

| Phase                                              | Time  |
| -------------------------------------------------- | ----- |
| Pull the image, 8.7 GB                             | 115 s |
| Container start to engine init                     | 52 s  |
| Download the weights, 8.8 GiB                      | 21 s  |
| Load the weights onto the GPU                      | 21 s  |
| Compile, profile memory, capture CUDA graphs       | 67 s  |
| Server up, next probe passes                       | 12 s  |

The image pull is the largest single cost, and every new node pays it. A restart on the same node keeps the image and the weights, but not the 44 s compile: its cache is in `/root/.cache/vllm`, outside the hostPath.

**The KV cache.** Chapter 2's formula for Qwen3-8B with the FP8 cache is 36 layers × 2 × 8 heads × 128 × 1 byte = 72 KiB per token, and 9.36 GiB / 72 KiB = 136,300 tokens: the arithmetic is exact. The 9.36 GiB is what the budget line leaves: vLLM may use 92% of the card, 20.52 GiB, and subtracts the weights (9.17 GiB), the peak activations of a full batch (1.99 GiB) and the CUDA graphs (0.56 GiB). Chapter 2's table subtracted only the weights, which gave 12.2 GB. That is 15 times section 1's 9,216 tokens. "Maximum concurrency 4.16x" assumes every request uses the full 32,768-token context; the sweep's requests use 1,094.

**The Marlin warning.** The A10G has no FP8 arithmetic. vLLM keeps the weights in FP8, so they take 8.9 GiB instead of 16, and widens them to BF16 inside each matmul. FP8 saves memory and bandwidth on this card, not compute. Section 7 shows where that matters.

## 6. One user

In one terminal:

```
kubectl port-forward svc/vllm 8000:8000
```

Start the metrics loop from section 1 in a second, then the chapter 3 single-stream benchmark with the hourly price:

```
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-8B-FP8 --price-per-hour 1.21
```

```
prompt=   44 completion= 128 ttft=     95 ms decode=  53.3 tok/s total=  2.5 s
prompt=   44 completion= 128 ttft=     61 ms decode=  53.2 tok/s total=  2.4 s
prompt=   44 completion= 128 ttft=     66 ms decode=  53.2 tok/s total=  2.5 s
prompt=   44 completion= 128 ttft=     65 ms decode=  53.2 tok/s total=  2.5 s
prompt=   44 completion= 128 ttft=     81 ms decode=  53.2 tok/s total=  2.5 s
median ttft 66 ms, median decode 53.2 tok/s
$1.21/h -> $6.32 per M output tokens, single stream
```

- 53.2 tok/s is 18.8 ms per token, 83% of chapter 2's 64 tok/s bandwidth ceiling for this card. The laptop ran a model 13 times smaller at 26.5 tok/s.
- 66 ms to first token includes the round trip from your machine to us-west-2.
- $6.32 per million tokens is the card's price divided by one stream's output. It is the most expensive way to run it.

The metrics loop shows `kv_cache_usage_perc` around 0.001 during each request. The cache is 136,272 / 16 = 8,517 blocks of 16 tokens, so 0.00117 is 10 blocks, about 160 tokens: the 44-token prompt and most of the 128 generated.

## 7. Many users

The section 1 sweep with the 8B, the price, and three more rows. About 6 minutes:

```
for n in 1 2 4 8 16 32 64 96 128; do
  uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-8B-FP8 --program --max-tokens 1024 --runs 1 --concurrency $n --price-per-hour 1.21
done 2>&1 | tee sweep.log
```

Reference run:

| N   | aggregate tok/s | goodput tok/s | in SLO  | median TTFT | median TPOT | wall   | $/M aggregate | $/M goodput |
| --- | --------------- | ------------- | ------- | ----------- | ----------- | ------ | ------------- | ----------- |
| 1   | 52.8            | 52.8          | 1/1     | 80 ms       | 18.9 ms     | 19.4 s | 6.35          | 6.35        |
| 2   | 104.7           | 104.7         | 2/2     | 100 ms      | 19.0 ms     | 19.6 s | 3.21          | 3.21        |
| 4   | 205.4           | 205.4         | 4/4     | 161 ms      | 19.3 ms     | 19.9 s | 1.64          | 1.64        |
| 8   | 398.3           | 398.3         | 8/8     | 211 ms      | 19.9 ms     | 20.6 s | 0.84          | 0.84        |
| 16  | 746.6           | 746.6         | 16/16   | 250 ms      | 21.2 ms     | 21.9 s | 0.45          | 0.45        |
| 32  | 1328.5          | 1328.5        | 32/32   | 606 ms      | 23.5 ms     | 24.7 s | 0.25          | 0.25        |
| 64  | 2017.4          | 914.2         | 29/64   | 1081 ms     | 30.7 ms     | 32.5 s | 0.17          | 0.37        |
| 96  | 1852.4          | 0             | 0/96    | 1098 ms     | 50.7 ms     | 53.1 s | 0.18          | inf         |
| 128 | 2171.6          | 0             | 0/128   | 1197 ms     | 57.3 ms     | 60.4 s | 0.155         | inf         |

**Batching is almost free up to 32.** Aggregate grows 25 times while each stream slows from 19 to 23.5 ms per token. Every decode step reads all 8.9 GiB of weights once, whether it serves one stream or 32. The laptop stopped at 75 tok/s by N=8; this card is still climbing at 64.

**The price per token falls 38 times on the same $1.21 card**, from $6.35 to $0.17 per million. What you pay per token depends on how many users share the card, more than on which card it is.

**Goodput picks a different N than throughput.** The SLO is 1 s to first token and 50 ms per token. At N=64, 35 requests miss the TTFT target. At 96 every request misses the TPOT target. The cheapest tokens that all meet the target are at N=32, $0.25 per million. Chapter 6's autoscaler aims for that point.

**Compute runs out before memory.** At N=96 the cache is 77% full, and every stream is over 50 ms per token: the batched matmuls, with their FP8-to-BF16 widening, are now the bottleneck. The aggregate dips below N=64 at this size; a second run gave the same numbers to within 0.1%.

**The cache fills at 128, exactly where the arithmetic says.** 128 requests × 1,094 tokens = 140,000, against 136,272. The metrics loop during N=128:

```
running   1.0 waiting  0.0 kv_cache_usage_perc 0.008 preemptions 0.0   <- warm-up request
running 104.0 waiting 24.0 kv_cache_usage_perc 0.051 preemptions 0.0   <- 24 still waiting for prefill
running 128.0 waiting  0.0 kv_cache_usage_perc 0.093 preemptions 0.0
running 128.0 waiting  0.0 kv_cache_usage_perc 0.514 preemptions 0.0
running 128.0 waiting  0.0 kv_cache_usage_perc 0.965 preemptions 0.0
running 126.0 waiting  2.0 kv_cache_usage_perc 0.994 preemptions 2.0   <- cache full, 2 evicted
running   0.0 waiting  0.0 kv_cache_usage_perc 0.000 preemptions 3.0
```

Section 1's eviction, at eight times the concurrency, in the last seconds of the run instead of halfway through.

**TTFT grows with N because prefills queue.** Each new request's prompt is prefilled before its first token, and the prefills share the GPU with every stream already decoding. 32, 64 and 128 prompts of 70 tokens divide out to about 4,000 prompt tokens per second at each N. vLLM's own TTFT metric agrees with the benchmark's, so the port-forward is not the cause (appendix C).

## 8. Delete everything

Stop the port-forward and the metrics loop, then:

```
time eksctl delete cluster -f chapters/05-moving-to-a-cloud-gpu-cluster/eks-cluster.yaml
```

About 15 minutes: eksctl drains and deletes the node group first, then the control plane and the network. Confirm nothing is left. Every command should print nothing:

```
aws eks list-clusters --region us-west-2 --output text
aws ec2 describe-instances --region us-west-2 --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text
aws ec2 describe-volumes --region us-west-2 --query 'Volumes[].VolumeId' --output text
aws cloudformation list-stacks --region us-west-2 --stack-status-filter CREATE_COMPLETE DELETE_FAILED \
  --query 'StackSummaries[].StackName' --output text
```

`DELETE_FAILED` is the one to look for: a stack that could not delete leaves billed resources behind. The reference session, from `create` to the end of `delete`:

| Item                      | Time        | Cost   |
| ------------------------- | ----------- | ------ |
| Control plane, $0.10/hour | about 1 h   | $0.10  |
| g5.2xlarge, $1.21/hour    | about 45 min | $0.91 |
| Total                     |             | about $1.01 |

The 100 GB disk adds cents. The next chapter starts from `eksctl create cluster` again.

## What the numbers say

1. **Compute and memory are both ceilings, and either can come first.** The laptop ran out of compute at N=8 and cache at N=16. The A10G ran out of compute at N=96 and cache at N=128. The cache size is predictable from chapter 2's arithmetic; the compute ceiling you measure.
2. **An idle card is the expensive mistake.** The same $1.21/hour card cost $6.35 per million tokens with one user and $0.17 with 64. Prices between providers differ by about 3 times; sharing the card changed the price per token 38 times.
3. **The SLO sets how many users a card should serve.** Throughput peaked at N=128 with no request inside the target. The answer for this card and this workload is N=32, and that is the number an autoscaler scales on.
4. **Read TTFT and TPOT separately.** TTFT is prefill waiting for the GPU, and it failed first, at N=64. TPOT is each decode step, and it failed at N=96. One latency number hides which of the two is struggling.

## Appendix

### A. The preemption counter lags

During the N=16 run, `vllm:num_preemptions_total` stayed flat while eight requests were evicted, then climbed by eight during readmission. vLLM records the preemption as an event on the request, and events reach the metrics logger with the request's next output token, which an evicted request does not produce until it is back in the batch. Read the counter after the run; use `kv_cache_usage_perc` and `num_requests_waiting` for the live view.

### B. What "out of stock" looks like

Three attempts, three error texts. On Nebius the console simply stopped listing the RTX PRO 6000 for node groups on 2026-09-21 while still offering it for managed endpoints, which are a separate, reserved pool. On AWS the autoscaling group behind the managed node group retries every two minutes and records each refusal; `eksctl` shows only "waiting for CloudFormation stack" the whole time, so this is the command that tells you what is happening:

```
aws autoscaling describe-scaling-activities --region us-west-2 \
  --auto-scaling-group-name $(aws autoscaling describe-auto-scaling-groups --region us-west-2 --query 'AutoScalingGroups[0].AutoScalingGroupName' --output text) \
  --max-items 3 --query 'Activities[].StatusMessage'
```

Spot, g7e.2xlarge, us-west-2b, 2026-10-13, while the spot price in that zone was listed at $1.78:

```
Could not launch Spot Instances. UnfulfillableCapacity - Unable to fulfill capacity due to your request configuration. Please adjust your request and try again.
```

On-demand, g7e.2xlarge, the same night, thirteen attempts across all four zones in twenty minutes, each one naming the other three zones as the fix:

```
Could not launch On-Demand Instances. InsufficientInstanceCapacity - We currently do not have sufficient g7e.2xlarge capacity in the Availability Zone you requested (us-west-2a). Our system will be working on provisioning additional capacity. You can currently get g7e.2xlarge capacity by not specifying an Availability Zone in your request or choosing us-west-2b, us-west-2c, us-west-2d.
```

A listed price is not a promise of a machine. The node group config in this chapter lists every zone, and the fallback is a one-line change of instance type to the card that is in stock.

### C. Server-side TTFT

The benchmark measures TTFT on your machine, through `kubectl port-forward`. vLLM measures it on the server. To compare the two, read the server's counters before and after one run:

```
curl -s localhost:8000/metrics | grep -E '^vllm:time_to_first_token_seconds_(sum|count)'
uv run python chapters/03-serving-with-vllm/benchmark.py --model Qwen/Qwen3-8B-FP8 --program --max-tokens 1024 --runs 1 --concurrency 96 --price-per-hour 1.21
curl -s localhost:8000/metrics | grep -E '^vllm:time_to_first_token_seconds_(sum|count)'
```

```
vllm:time_to_first_token_seconds_count{...} 372.0
vllm:time_to_first_token_seconds_sum{...} 311.4352025985718
concurrency= 96 aggregate= 1852.4 tok/s goodput=    0.0 tok/s (0/96 in SLO) wall=  53.1 s | ttft median= 1098 p99= 1646 ms | ...
vllm:time_to_first_token_seconds_count{...} 469.0
vllm:time_to_first_token_seconds_sum{...} 406.74579644203186
```

The count rose by 97, the 96 requests plus the benchmark's warm-up. The sum rose by 95.3 s, a mean of 0.98 s on the server against a 1.10 s median on your machine. The tunnel adds about 0.1 s; the rest is the server.
