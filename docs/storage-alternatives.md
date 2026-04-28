# Storage alternatives for AI workloads on AKS

This repo intentionally benchmarks storage by access pattern. AI workloads often
need several storage tiers at once: durable object storage, shared file storage,
node-local scratch, and sometimes a cache layer.

## Alternatives matrix

| Option | Shape | Strengths | Watch-outs | Best first benchmark |
|---|---|---|---|---|
| Azure Blob CSI / BlobFuse | Object storage mounted as files | Durable, cheap, shared, cloud-native source of truth | FUSE/object semantics; metadata/list/rename and many-small-file workloads can be slow or cache-sensitive | Small-file read/list and large sequential reads |
| Azure Blob Storage NFS v3 | Blob account exposed through NFS | Shared RWX filesystem semantics over Azure storage; good for active pod-to-pod file sharing | NFS semantics/limits; region/network placement matters | Checkpoint-like writes, small-file list/read |
| Azure Files Premium | Managed SMB/NFS file share | Simple managed RWX share; easy Kubernetes integration | Throughput/IOPS scale with provisioned size; not a high-end parallel FS | Mixed small files and moderate checkpoint writes |
| Azure NetApp Files | Managed enterprise NFS/SMB | High throughput/low latency shared filesystem; snapshots; mature NFS behavior | Requires capacity pool planning and network setup; higher cost floor | Large read/write and checkpoint-like writes |
| Azure Managed Disk / Ultra Disk | RWO block device | Strong single-node latency/throughput; predictable disk perf | Not RWX; data follows one node/pod unless copied | Single-pod checkpoint writes and model staging |
| Local NVMe / `emptyDir` | Ephemeral node-local storage | Fastest common option; ideal for scratch and unpacked caches | Disappears with pod/node; not shared | Upper-bound baseline for reads/writes |
| Alluxio over Blob/ADLS | Distributed cache/namespace | Warm-cache repeated reads; can use RAM/NVMe near compute; avoids repeated object-store latency | Extra distributed system; eviction and consistency choices; write behavior needs care | Warm vs cold large model/dataset reads |
| Lustre / Azure Managed Lustre | Parallel filesystem | High aggregate throughput for large distributed training jobs | Operational/cost complexity; best for larger GPU fleets | Multi-node large-file streaming |
| WEKA | High-performance distributed filesystem | Very high throughput/metadata performance for AI/HPC | Commercial dependency and operational footprint | Multi-node mixed metadata + large-file workloads |
| Ceph / Rook | Self-managed distributed storage | Flexible block/file/object in Kubernetes | Heavy ops burden; failure-domain design matters | RWX file benchmarks and recovery tests |
| MinIO / object gateway | S3-compatible object layer | Portable object API; useful for artifact contracts | Not POSIX; apps need object semantics or FUSE/gateway layer | S3 GET/PUT throughput, not file rename tests |

## Practical guidance

Start with the smallest set that matches the workload:

| Workload need | Sensible default |
|---|---|
| Durable datasets, archives, artifacts | Blob/ADLS |
| Active shared outputs, checkpoints, adapter dirs, leaderboards | NFS/ANF/Azure Files depending on scale |
| Single-node preprocessing or unpacked model cache | local NVMe/`emptyDir` |
| Many jobs repeatedly reading the same immutable files | NFS first, then benchmark Alluxio or another cache |
| Large multi-node training where storage feeds many GPUs | benchmark ANF, Lustre, WEKA, or a dedicated cache tier |

## Why raw GB/s and Gbps both matter

Training teams usually care about application throughput in GB/s or GiB/s, while
platform teams often reason about NIC saturation in Gbps. The benchmark reports:

- `GB/s`: decimal gigabytes per second, useful for comparing with cloud storage
  marketing/spec sheets.
- `GiB/s`: binary gibibytes per second, useful for file sizes reported by Linux
  tools.
- `Gbps`: gigabits per second, useful for checking whether the storage path is
  network-bound.

Use the raw per-iteration samples to spot warm-cache effects and outliers before
trusting a median summary.

