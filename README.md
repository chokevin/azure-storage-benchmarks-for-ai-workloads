# Azure Storage Benchmarks for AI Workloads

Unofficial, reproducible benchmarks for comparing storage choices used by AI
training and inference jobs on AKS.

The goal is not to crown one storage system universally. The goal is to make it
easy to answer: **is storage starving my GPU workload, and which mount should I
use for this access pattern?**

## What this measures

The benchmark CLI exercises patterns that commonly hurt AI jobs:

| Pattern | Why it matters |
|---|---|
| Small-file create/read/list | Tokenized datasets, manifests, eval outputs, metadata-heavy pipelines |
| Large-file sequential read/write | Model weights, checkpoints, archives |
| Checkpoint-like atomic writes | Write temp file, flush, rename into place |
| Async checkpoint-like writes | Submit checkpoint writes to a background writer and measure caller-blocked time vs writer time |
| Raw transfer samples | Per-iteration bytes, seconds, GB/s, GiB/s, and Gbps |
| Mount identity | Filesystem type and mount source from `/proc/self/mountinfo` when available |
| Markdown/JSON reporting | Easy comparison across mounts and clusters |

Typical AKS targets:

| Target | Example mount | Expected use |
|---|---|---|
| Azure Blob CSI / BlobFuse | `/data` | Durable shared source of truth |
| Azure Blob Storage NFS v3 | `/data-nfs` | Shared filesystem for active writes/results |
| Azure Files Premium | `/azure-files` | General RWX file share, usually simpler than high-end parallel filesystems |
| Azure NetApp Files | `/anf` | High-performance shared filesystem with managed NFS/SMB |
| Managed Disk / Ultra Disk | `/mnt/disk` | Fast single-node RWO block storage |
| Node-local scratch / NVMe | `/scratch` | Fast ephemeral cache or staging |
| Alluxio cache over Blob/ADLS | `/data-alluxio` | Optional warm read cache for repeated model/dataset reads |

See [docs/storage-alternatives.md](docs/storage-alternatives.md) for a broader
alternatives matrix, including Lustre-style filesystems, WEKA, MinIO, Ceph, and
cache layers.

See [docs/voice-agent-flex-results.md](docs/voice-agent-flex-results.md) for a
real `voice-agent-flex` capture that profiles a voice/autoresearch small-file
path and compares BlobFuse, Blob NFS v3, Azure Disk, and local scratch.

See [docs/rune-azure-storage-defaults.md](docs/rune-azure-storage-defaults.md)
for recommended Rune job defaults on Azure: Blob as durable storage, `/mnt` as
the hot read/checkpoint path, async durable checkpoint copy, and the benchmark
roadmap for refining those defaults.

## Quick start: local smoke test

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
azure-storage-benchmark run \
  --path local=/tmp/azure-storage-benchmark \
  --small-files 100 \
  --large-file-size-mib 16 \
  --checkpoint-size-mib 8 \
  --output-json storage-benchmark-results.json \
  --output-md storage-benchmark-results.md
```

## Quick start: AKS Job

Build and publish an image, or replace the image in
`examples/kubernetes/storage-benchmark-job.yaml` with your own:

```bash
docker build -t ghcr.io/chokevin/azure-storage-benchmarks-for-ai-workloads:latest .
docker push ghcr.io/chokevin/azure-storage-benchmarks-for-ai-workloads:latest

kubectl apply -f examples/kubernetes/storage-benchmark-job.yaml
kubectl -n ray logs job/azure-storage-benchmark
```

The example Job compares:

- `blob=/data` from PVC `blob-training`
- `nfs=/data-nfs` from PVC `training-nfs`
- `local=/scratch` from `emptyDir`

Adjust PVC names, namespace, image, and sizes for your cluster.

Additional examples:

- `examples/kubernetes/azure-disk-options-job.yaml` creates temporary Azure Disk
  PVCs for Standard SSD and Premium SSD StorageClasses.
- `examples/kubernetes/azure-blob-nfs-v3-job.yaml` mounts a real Azure Blob NFS
  v3 export. This requires storage account firewall/VNet setup before applying.
- `examples/kubernetes/lustre-training-data-read-job.yaml` runs read-only
  Lustre CSI dataset scans for 1 TiB, 5 TiB, 10 TiB, and 50 TiB training-data
  tiers on `aks-ai-runtime-flex`-style clusters (the Lustre/AMLFS mount lives on
  the eastuseuap H200 nodes joined via Flex).
- `examples/kubernetes/amlfs-elastic-validation-job.yaml` validates the AMLFS
  mount and HSM observability across specific flex nodes before relying on an
  elastic GPU label.
- `examples/kubernetes/dolma-amlfs-stage-and-benchmark-job.yaml` stages an
  explicitly capped slice of the open Dolma pretraining corpus onto AMLFS and
  benchmarks it. Increase the URL cap only when you are ready for multi-TB
  downloads. A live run of the default 100-URL slice (~174 GB of Dolma v1.7
  `books`/`c4` shards) read at 3.810 GB/s on a `voice-agent-flex` EUAP H200
  node; see `docs/voice-agent-flex-results.md`.
- `examples/pytorch/gpt2_async_checkpoint.py` trains a tiny GPT-style PyTorch
  model on Hugging Face text data and reports sync vs async checkpoint behavior,
  separating pure training-step throughput from loop throughput that includes
  checkpoint waits. `examples/kubernetes/gpt2-async-checkpoint-gpu-skus.yaml`
  runs that sample once per GPU SKU label (`a100`, `h100`, `h200`) with 2 GiB
  checkpoint padding on clusters with matching DRA GPU resource claims.

## CLI

```bash
azure-storage-benchmark run \
  --path blob=/data \
  --path nfs=/data-nfs \
  --path local=/scratch \
  --small-files 1000 \
  --small-file-size-kib 4 \
  --large-file-size-mib 1024 \
  --checkpoint-files 4 \
  --checkpoint-size-mib 256 \
  --async-checkpoint \
  --async-checkpoint-overlap-ms 1000 \
  --iterations 3 \
  --output-json /data-nfs/storage-benchmarks/results.json \
  --output-md /data-nfs/storage-benchmarks/results.md
```

For existing training datasets, use the read-only `dataset-read` command. It
recursively enumerates files in deterministic path order, reads with bounded
parallelism, and reports enumeration time separately from read throughput:

```bash
azure-storage-benchmark dataset-read \
  --path lustre=/lustre/training-data/1tib \
  --max-bytes 1099511627776 \
  --concurrency 16 \
  --block-size-mib 16 \
  --run-id lustre-training-data-read-1tib \
  --output-json /data/storage-benchmarks/lustre-training-data-read-1tib.json \
  --output-md /data/storage-benchmarks/lustre-training-data-read-1tib.md
```

`--max-bytes` is a per-path cap. The command may stop partway through the final
file to hit that cap; the JSON records selected files and `bytes_to_read` for
reproducibility. For Lustre mounts, the report also captures `lfs df -h` and
`lfs getstripe` output when the `lfs` client tool is available in the image.
On multi-network GPU clusters, pin Lustre jobs to nodes that can actually route
to the Lustre MGS instead of relying on a broad GPU label.

For AMLFS designs that depend on HSM tiering and elastic node reallocation, run
the lighter validation probe before the full read:

```bash
azure-storage-benchmark amlfs-validate \
  --path amlfs=/lustre \
  --sample-files 1000 \
  --output-json /data/storage-benchmarks/amlfs-validation.json \
  --output-md /data/storage-benchmarks/amlfs-validation.md
```

This records mount identity, sampled dataset shape, and whether `lfs hsm_state`
is available from the pod image. Use an image with Lustre client tools for HSM
state validation; otherwise the report will explicitly mark HSM commands as
unavailable.

Use `--keep-data` if you want to inspect the generated files. Otherwise the CLI
deletes its per-run directory after each path completes.

The JSON report contains raw transfer samples under:

```text
results[].raw.transfer_samples[]
```

Each sample includes `operation`, `bytes`, `seconds`, decimal `gb_s`, binary
`gib_s`, and network-style `gbps`. The Markdown report renders the same raw
samples below the summary table.

The summary table separates **first read** from **warm read** throughput. First
read is the closest signal this simple benchmark has for cold-ish storage access.
Warm reads are often served from kernel page cache, FUSE cache, or a storage
client cache and can look like local memory/disk instead of remote storage.

When `--async-checkpoint` is set, the benchmark also writes a checkpoint-like
payload in a background thread. `Async writer GB/s` is the actual background
write throughput. `Async blocked GB/s` uses only the time the caller spent
submitting the write plus waiting after the configured
`--async-checkpoint-overlap-ms` compute window. If the writer finishes during
that overlap window, blocked throughput can look much higher than physical
storage throughput; that is the point of async checkpointing, but only if the
training step has enough work to hide the write.

Also check the `FS type` and `Mount source` columns. If two paths report the
same filesystem type/source, you may be comparing two aliases for the same
backend rather than two independent storage systems.

## Reading results

Prefer matching the storage to the workload:

| Workload shape | Usually start with |
|---|---|
| Durable source datasets and archives | Blob/ADLS |
| Hot checkpoints, adapters, leaderboards, frequent renames | NFS/shared filesystem |
| Many tiny cold files in a single-pod training or preprocessing loop | Stage to local scratch or Azure Disk |
| Repeated read-only model/data access | NFS or a cache layer such as Alluxio |
| Temporary preprocessing on one node | local NVMe/`emptyDir` |

Measure before changing the platform. Cache layers can improve repeated reads,
but they also add operations, eviction, consistency, and failure-mode questions.

## Disclaimer

This is an unofficial experimental benchmark harness. It is not an Azure product
recommendation and does not imply support for any specific storage layout.
