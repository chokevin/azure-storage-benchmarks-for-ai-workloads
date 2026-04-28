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
  --iterations 3 \
  --output-json /data-nfs/storage-benchmarks/results.json \
  --output-md /data-nfs/storage-benchmarks/results.md
```

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

Also check the `FS type` and `Mount source` columns. If two paths report the
same filesystem type/source, you may be comparing two aliases for the same
backend rather than two independent storage systems.

## Reading results

Prefer matching the storage to the workload:

| Workload shape | Usually start with |
|---|---|
| Durable source datasets and archives | Blob/ADLS |
| Hot checkpoints, adapters, leaderboards, frequent renames | NFS/shared filesystem |
| Repeated read-only model/data access | NFS or a cache layer such as Alluxio |
| Temporary preprocessing on one node | local NVMe/`emptyDir` |

Measure before changing the platform. Cache layers can improve repeated reads,
but they also add operations, eviction, consistency, and failure-mode questions.

## Disclaimer

This is an unofficial experimental benchmark harness. It is not an Azure product
recommendation and does not imply support for any specific storage layout.
