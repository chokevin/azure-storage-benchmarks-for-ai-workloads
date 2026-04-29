# voice-agent-flex benchmark notes

This page records the real workload-shaped benchmark runs from the
`voice-agent-flex` AKS cluster. These are not generic storage claims; they are
evidence for one observed voice/autoresearch access pattern.

## Why this was measured

Heavy transcription and voice-agent training work appeared slow when reading
from the Blob-backed `/data` mount. The question was whether Blob itself was
slow for bulk reads, or whether the workload was hitting a more specific
small-file metadata/read pattern.

The investigation found that the hot path was not the STT serving pod directly:

- `fish-speech`, `gura-bot`, `gura-llm`, and `hutao-bot` mount
  `blob-training` at `/data`.
- `stt-serving` does not mount the Blob PVC.
- The autoresearch training scripts reference paths such as
  `/data/training/autoresearch`, `/data/autoresearch/results/...`, and
  `/data/replay-logs`.

## Data shape

The slow path is dominated by many tiny JSON/result artifacts under
`/data/autoresearch`, especially `/data/autoresearch/results/gura`.

| Path | Files | Bytes | Shape |
|---|---:|---:|---|
| `/data/autoresearch` | 11,440 | 13,219,591,738 | 10,298 files are under 4 KiB; mostly JSON plus model/tokenizer artifacts |
| `/data/autoresearch/results/gura` | 3,224 | 19,794,106 | 2,427 files are under 4 KiB; 778 files are 4-64 KiB |
| `/data/training/autoresearch` | 97 | 268,636,529 | Mostly JSONL training files; not the main tiny-file hotspot |
| `/data/replay-logs` | 6 | 24,088,736 | SQLite database files |

The benchmark samples below use the `gura` results directory because it
captures the painful tiny-file shape without reading the whole container.

## Reproduction-oriented profile

Artifact on the cluster:

```text
/data/storage-benchmarks/voice-path-profile-202604291825.{json,md}
```

The profile read up to 3,000 small files from each candidate path and compared
BlobFuse first-pass reads, BlobFuse warm reads, and the same staged sample on
node-local scratch.

| Path | BlobFuse first pass | BlobFuse warm pass | Local staged sample |
|---|---:|---:|---:|
| `/data/autoresearch` | 134 files/s, 0.326 MB/s | 8,280 files/s, 20.1 MB/s | 99k files/s, 241 MB/s |
| `/data/autoresearch/results/gura` | 135 files/s, 0.341 MB/s | 8,316 files/s, 21.0 MB/s | 96k files/s, 244 MB/s |

Interpretation: the observed slowness is cold small-file access through
BlobFuse. Warm-cache numbers are much better, which means repeated reads can
hide the issue during local testing or later epochs.

## Storage type comparison

Artifact on the cluster:

```text
/data/storage-benchmarks/voice-storage-compare-202604291833.{json,md}
```

This comparison sampled 3,000 files from
`/data/autoresearch/results/gura`, totaling 8,001,400 bytes with each file under
64 KiB. The exact sample was then staged to each target before measuring list
and read behavior.

| Target | FS | Stage s | List files/s | First read files/s | Warm read files/s | First read MB/s | Warm read MB/s |
|---|---|---:|---:|---:|---:|---:|---:|
| BlobFuse source | fuse |  | 7,423 | 140 | 8,144 | 0.861 | 50.001 |
| Blob NFS v3 | nfs | 130.789 | 891 | 155 | 183 | 0.412 | 0.487 |
| Standard Azure Disk | ext4 | 30.486 | 231,674 | 100,594 | 104,789 | 268.298 | 279.487 |
| Premium Azure Disk | ext4 | 0.622 | 230,758 | 101,902 | 105,302 | 271.787 | 280.853 |
| local `emptyDir` | ext4 | 0.615 | 241,639 | 99,449 | 101,909 | 265.242 | 271.804 |

Ranking for this access pattern:

```text
Azure Disk ~= local scratch >>> warm BlobFuse >>> cold BlobFuse ~= Blob NFS v3
```

Blob NFS v3 helped the earlier directory-listing-heavy synthetic run, but it did
not help this tiny-file read/open pattern. For this voice/autoresearch sample,
the practical fix is to stage or pack the tiny files before heavy training or
transcription loops.

## Caveats

- The staged targets are measured after copying the same sampled files from
  BlobFuse. That makes target reads warmer than a completely cold dataset
  provisioned natively on those backends, but it preserves the exact file shape
  of the voice result directory.
- Azure Disk and local `emptyDir` are single-node options in these runs. They
  are strong staging/cache targets, not shared durable sources of truth.
- BlobFuse warm-cache results depend on cache state, pod placement, and client
  behavior. Treat first-pass numbers as the safer signal for new jobs or cold
  nodes.
- The `training-nfs` PVC in this cluster is also a Blob CSI / BlobFuse mount to
  the same backing container, so it should not be interpreted as a real NFS
  comparison.

