from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MIB = 1024 * 1024
KIB = 1024
GB = 1000 * 1000 * 1000
GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class BenchmarkConfig:
    small_files: int = 1000
    small_file_size_kib: int = 4
    large_file_size_mib: int = 256
    checkpoint_files: int = 4
    checkpoint_size_mib: int = 64
    async_checkpoint: bool = False
    async_checkpoint_overlap_ms: int = 0
    iterations: int = 3
    run_id: str | None = None
    keep_data: bool = False

    def validate(self) -> None:
        positive_ints = {
            "small_files": self.small_files,
            "small_file_size_kib": self.small_file_size_kib,
            "large_file_size_mib": self.large_file_size_mib,
            "checkpoint_files": self.checkpoint_files,
            "checkpoint_size_mib": self.checkpoint_size_mib,
            "iterations": self.iterations,
        }
        for name, value in positive_ints.items():
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        if self.async_checkpoint_overlap_ms < 0:
            raise ValueError(
                "async_checkpoint_overlap_ms must be >= 0, "
                f"got {self.async_checkpoint_overlap_ms}"
            )


def parse_named_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"path must be NAME=/mount/path, got {value!r}")
    name, raw_path = value.split("=", 1)
    name = name.strip()
    raw_path = raw_path.strip()
    if not name:
        raise ValueError(f"path name is empty in {value!r}")
    if not raw_path:
        raise ValueError(f"path value is empty in {value!r}")
    return name, Path(raw_path)


def run_benchmarks(paths: dict[str, Path], config: BenchmarkConfig) -> dict:
    config.validate()
    run_id = config.run_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    results = {
        "schema_version": "4",
        "run_id": run_id,
        "started_at": started_at,
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "config": {
            "small_files": config.small_files,
            "small_file_size_kib": config.small_file_size_kib,
            "large_file_size_mib": config.large_file_size_mib,
            "checkpoint_files": config.checkpoint_files,
            "checkpoint_size_mib": config.checkpoint_size_mib,
            "async_checkpoint": config.async_checkpoint,
            "async_checkpoint_overlap_ms": config.async_checkpoint_overlap_ms,
            "iterations": config.iterations,
            "keep_data": config.keep_data,
        },
        "results": [],
    }

    for name, base_path in paths.items():
        results["results"].append(run_one_path(name, base_path, run_id, config))

    return results


def run_one_path(name: str, base_path: Path, run_id: str, config: BenchmarkConfig) -> dict:
    path_result = {
        "name": name,
        "path": str(base_path),
        "mount": _mount_info_for(base_path),
        "ok": False,
        "metrics": {},
        "raw": {},
        "error": "",
    }

    if not base_path.exists():
        path_result["error"] = "path does not exist"
        return path_result
    if not base_path.is_dir():
        path_result["error"] = "path is not a directory"
        return path_result

    run_root = base_path / f"asb-{run_id}-{name}"
    try:
        run_root.mkdir(parents=True, exist_ok=False)
        small_dir = run_root / "small-files"
        large_dir = run_root / "large-files"
        checkpoint_dir = run_root / "checkpoint-like"
        async_checkpoint_dir = run_root / "async-checkpoint-like"
        small_dir.mkdir()
        large_dir.mkdir()
        checkpoint_dir.mkdir()
        if config.async_checkpoint:
            async_checkpoint_dir.mkdir()

        raw = {}
        raw["small_write_seconds"] = _time_call(
            lambda: _write_small_files(
                small_dir,
                count=config.small_files,
                size_bytes=config.small_file_size_kib * KIB,
            )
        )
        raw["list_seconds"] = [
            _time_call(lambda: _list_files(small_dir, expected=config.small_files))
            for _ in range(config.iterations)
        ]
        raw["small_read_seconds"] = [
            _time_call(lambda: _read_files(sorted(small_dir.iterdir())))
            for _ in range(config.iterations)
        ]

        large_file = large_dir / "large.bin"
        raw["large_write_seconds"] = _time_call(
            lambda: _write_file(large_file, config.large_file_size_mib * MIB)
        )
        raw["large_read_seconds"] = [
            _time_call(lambda: _read_files([large_file])) for _ in range(config.iterations)
        ]

        raw["checkpoint_write_seconds"] = _time_call(
            lambda: _write_checkpoint_like(
                checkpoint_dir,
                files=config.checkpoint_files,
                size_bytes=config.checkpoint_size_mib * MIB,
            )
        )
        if config.async_checkpoint:
            raw["async_checkpoint"] = _write_async_checkpoint_like(
                async_checkpoint_dir,
                files=config.checkpoint_files,
                size_bytes=config.checkpoint_size_mib * MIB,
                overlap_seconds=config.async_checkpoint_overlap_ms / 1000,
            )

        small_total_bytes = config.small_files * config.small_file_size_kib * KIB
        large_total_bytes = config.large_file_size_mib * MIB
        checkpoint_total_bytes = config.checkpoint_files * config.checkpoint_size_mib * MIB
        small_read_first_seconds = raw["small_read_seconds"][0]
        small_read_warm_seconds = _median_tail_or_all(raw["small_read_seconds"])
        large_read_first_seconds = raw["large_read_seconds"][0]
        large_read_warm_seconds = _median_tail_or_all(raw["large_read_seconds"])
        raw["transfer_samples"] = [
            _transfer_sample(
                operation="small_write",
                bytes_count=small_total_bytes,
                seconds=raw["small_write_seconds"],
            ),
            *[
                _transfer_sample(
                    operation="small_read",
                    bytes_count=small_total_bytes,
                    seconds=seconds,
                    iteration=index,
                )
                for index, seconds in enumerate(raw["small_read_seconds"], start=1)
            ],
            _transfer_sample(
                operation="large_write",
                bytes_count=large_total_bytes,
                seconds=raw["large_write_seconds"],
            ),
            *[
                _transfer_sample(
                    operation="large_read",
                    bytes_count=large_total_bytes,
                    seconds=seconds,
                    iteration=index,
                )
                for index, seconds in enumerate(raw["large_read_seconds"], start=1)
            ],
            _transfer_sample(
                operation="checkpoint_write",
                bytes_count=checkpoint_total_bytes,
                seconds=raw["checkpoint_write_seconds"],
            ),
        ]
        if config.async_checkpoint:
            async_raw = raw["async_checkpoint"]
            raw["transfer_samples"].extend(
                [
                    _transfer_sample(
                        operation="async_checkpoint_writer",
                        bytes_count=checkpoint_total_bytes,
                        seconds=async_raw["writer_seconds"],
                    ),
                    _transfer_sample(
                        operation="async_checkpoint_blocked",
                        bytes_count=checkpoint_total_bytes,
                        seconds=async_raw["caller_blocked_seconds"],
                    ),
                ]
            )

        metrics = {
            "small_write_ms_per_file": _ms_per_item(raw["small_write_seconds"], config.small_files),
            "small_read_first_ms_per_file": _ms_per_item(
                small_read_first_seconds, config.small_files
            ),
            "small_read_warm_ms_per_file": _ms_per_item(
                small_read_warm_seconds, config.small_files
            ),
            "small_read_ms_per_file": _ms_per_item(
                statistics.median(raw["small_read_seconds"]), config.small_files
            ),
            "small_read_mib_s": _mib_per_second(
                small_total_bytes, statistics.median(raw["small_read_seconds"])
            ),
            "list_ms_per_1000_files": _ms_per_1000(
                statistics.median(raw["list_seconds"]), config.small_files
            ),
            "large_write_mib_s": _mib_per_second(large_total_bytes, raw["large_write_seconds"]),
            "large_write_gb_s": _gb_per_second(large_total_bytes, raw["large_write_seconds"]),
            "large_write_gbps": _gbps(large_total_bytes, raw["large_write_seconds"]),
            "large_read_mib_s": _mib_per_second(
                large_total_bytes, statistics.median(raw["large_read_seconds"])
            ),
            "large_read_first_gb_s": _gb_per_second(
                large_total_bytes, large_read_first_seconds
            ),
            "large_read_warm_gb_s": _gb_per_second(
                large_total_bytes, large_read_warm_seconds
            ),
            "large_read_gb_s": _gb_per_second(
                large_total_bytes, statistics.median(raw["large_read_seconds"])
            ),
            "large_read_gbps": _gbps(
                large_total_bytes, statistics.median(raw["large_read_seconds"])
            ),
            "checkpoint_write_mib_s": _mib_per_second(
                checkpoint_total_bytes, raw["checkpoint_write_seconds"]
            ),
            "checkpoint_write_gb_s": _gb_per_second(
                checkpoint_total_bytes, raw["checkpoint_write_seconds"]
            ),
            "checkpoint_write_gbps": _gbps(
                checkpoint_total_bytes, raw["checkpoint_write_seconds"]
            ),
        }
        if config.async_checkpoint:
            async_raw = raw["async_checkpoint"]
            metrics.update(
                {
                    "async_checkpoint_submit_ms": async_raw["submit_seconds"] * 1000,
                    "async_checkpoint_overlap_ms": async_raw["overlap_seconds"] * 1000,
                    "async_checkpoint_wait_ms": async_raw["wait_seconds"] * 1000,
                    "async_checkpoint_blocked_ms": async_raw["caller_blocked_seconds"] * 1000,
                    "async_checkpoint_writer_gb_s": _gb_per_second(
                        checkpoint_total_bytes, async_raw["writer_seconds"]
                    ),
                    "async_checkpoint_blocked_gb_s": _gb_per_second(
                        checkpoint_total_bytes, async_raw["caller_blocked_seconds"]
                    ),
                }
            )

        path_result["ok"] = True
        path_result["metrics"] = {k: round(v, 3) for k, v in metrics.items()}
        path_result["raw"] = raw
        return path_result
    except Exception as exc:  # noqa: BLE001 - surface per-path failures in JSON report.
        path_result["error"] = f"{type(exc).__name__}: {exc}"
        return path_result
    finally:
        if not config.keep_data:
            shutil.rmtree(run_root, ignore_errors=True)


def write_json_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_markdown(report: dict) -> str:
    lines = [
        "# Storage Benchmark Results",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- Started: `{report['started_at']}`",
        f"- Host: `{report['host']['hostname']}`",
        "",
        "| Target | OK | FS type | Mount source | Small read first ms/file | Small read warm ms/file | List ms/1000 files | Large read first GB/s | Large read warm GB/s | Large write GB/s | Checkpoint write GB/s | Async submit ms | Async wait ms | Async blocked GB/s | Async writer GB/s | Error |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in report["results"]:
        metrics = result.get("metrics", {})
        mount = result.get("mount") or {}
        lines.append(
            "| {name} | {ok} | {fs_type} | {source} | {small_read_first} | {small_read_warm} | {list_ms} | {large_read_first} | {large_read_warm} | {large_write} | {checkpoint_write} | {async_submit} | {async_wait} | {async_blocked} | {async_writer} | {error} |".format(
                name=result["name"],
                ok="yes" if result["ok"] else "no",
                fs_type=(mount.get("fs_type") or "").replace("|", "\\|"),
                source=(mount.get("source") or "").replace("|", "\\|"),
                small_read_first=_fmt_metric(metrics.get("small_read_first_ms_per_file")),
                small_read_warm=_fmt_metric(metrics.get("small_read_warm_ms_per_file")),
                list_ms=_fmt_metric(metrics.get("list_ms_per_1000_files")),
                large_read_first=_fmt_metric(metrics.get("large_read_first_gb_s")),
                large_read_warm=_fmt_metric(metrics.get("large_read_warm_gb_s")),
                large_write=_fmt_metric(metrics.get("large_write_gb_s")),
                checkpoint_write=_fmt_metric(metrics.get("checkpoint_write_gb_s")),
                async_submit=_fmt_metric(metrics.get("async_checkpoint_submit_ms")),
                async_wait=_fmt_metric(metrics.get("async_checkpoint_wait_ms")),
                async_blocked=_fmt_metric(metrics.get("async_checkpoint_blocked_gb_s")),
                async_writer=_fmt_metric(metrics.get("async_checkpoint_writer_gb_s")),
                error=(result.get("error") or "").replace("|", "\\|"),
            )
        )
    lines.append("")
    lines.append("Lower latency is better for `ms/*` columns. Higher throughput is better for `GB/s` and `Gbps` columns.")
    lines.append("")
    lines.extend(_render_transfer_samples(report))
    lines.append("")
    return "\n".join(lines)


def write_markdown_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")


def _fmt_metric(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _time_call(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def _median_tail_or_all(values: list[float]) -> float:
    if len(values) > 1:
        return statistics.median(values[1:])
    return statistics.median(values)


def _write_small_files(directory: Path, count: int, size_bytes: int) -> None:
    payload = _payload(size_bytes)
    for index in range(count):
        _write_file(directory / f"file-{index:06d}.bin", size_bytes, payload=payload)


def _write_checkpoint_like(directory: Path, files: int, size_bytes: int) -> None:
    for index in range(files):
        tmp_path = directory / f"checkpoint-{index:03d}.tmp"
        final_path = directory / f"checkpoint-{index:03d}.bin"
        _write_file(tmp_path, size_bytes)
        os.replace(tmp_path, final_path)


def _write_async_checkpoint_like(
    directory: Path,
    files: int,
    size_bytes: int,
    overlap_seconds: float,
) -> dict:
    submit_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _time_call,
            lambda: _write_checkpoint_like(directory, files=files, size_bytes=size_bytes),
        )
        submit_seconds = time.perf_counter() - submit_start
        if overlap_seconds > 0:
            time.sleep(overlap_seconds)
        wait_start = time.perf_counter()
        writer_seconds = future.result()
        wait_seconds = time.perf_counter() - wait_start

    return {
        "submit_seconds": submit_seconds,
        "overlap_seconds": overlap_seconds,
        "wait_seconds": wait_seconds,
        "writer_seconds": writer_seconds,
        "caller_blocked_seconds": submit_seconds + wait_seconds,
    }


def _write_file(path: Path, size_bytes: int, payload: bytes | None = None) -> None:
    chunk = payload or _payload(min(size_bytes, MIB))
    remaining = size_bytes
    with path.open("wb") as handle:
        while remaining:
            to_write = chunk if remaining >= len(chunk) else chunk[:remaining]
            handle.write(to_write)
            remaining -= len(to_write)
        handle.flush()
        os.fsync(handle.fileno())


def _read_files(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(MIB)
                if not chunk:
                    break
                total += len(chunk)
    return total


def _list_files(directory: Path, expected: int) -> None:
    entries = list(directory.iterdir())
    if len(entries) != expected:
        raise RuntimeError(f"expected {expected} entries, found {len(entries)}")


def _payload(size_bytes: int) -> bytes:
    pattern = b"azure-storage-benchmark\n"
    repeats, remainder = divmod(size_bytes, len(pattern))
    return pattern * repeats + pattern[:remainder]


def _ms_per_item(seconds: float, items: int) -> float:
    return (seconds * 1000) / items


def _ms_per_1000(seconds: float, items: int) -> float:
    return (seconds * 1000) * (1000 / items)


def _mib_per_second(bytes_count: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return (bytes_count / MIB) / seconds


def _mount_info_for(path: Path) -> dict:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return {}

    resolved_path = path.resolve()
    best_match: dict | None = None
    best_length = -1
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        parsed = _parse_mountinfo_line(line)
        if not parsed:
            continue
        mount_point = Path(parsed["mount_point"])
        if (
            _path_is_at_or_below(resolved_path, mount_point)
            and len(parsed["mount_point"]) > best_length
        ):
            best_match = parsed
            best_length = len(parsed["mount_point"])
    return best_match or {}


def _parse_mountinfo_line(line: str) -> dict | None:
    if " - " not in line:
        return None
    left, right = line.split(" - ", 1)
    left_fields = left.split()
    right_fields = right.split()
    if len(left_fields) < 5 or len(right_fields) < 2:
        return None
    return {
        "mount_point": _decode_mountinfo_field(left_fields[4]),
        "root": _decode_mountinfo_field(left_fields[3]),
        "fs_type": _decode_mountinfo_field(right_fields[0]),
        "source": _decode_mountinfo_field(right_fields[1]),
    }


def _decode_mountinfo_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _path_is_at_or_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _gb_per_second(bytes_count: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return (bytes_count / GB) / seconds


def _gib_per_second(bytes_count: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return (bytes_count / GIB) / seconds


def _gbps(bytes_count: int, seconds: float) -> float:
    return _gb_per_second(bytes_count, seconds) * 8


def _transfer_sample(
    operation: str,
    bytes_count: int,
    seconds: float,
    iteration: int | None = None,
) -> dict:
    sample = {
        "operation": operation,
        "bytes": bytes_count,
        "seconds": seconds,
        "mb_s": (bytes_count / (1000 * 1000)) / seconds if seconds > 0 else 0.0,
        "mib_s": _mib_per_second(bytes_count, seconds),
        "gb_s": _gb_per_second(bytes_count, seconds),
        "gib_s": _gib_per_second(bytes_count, seconds),
        "gbps": _gbps(bytes_count, seconds),
    }
    if iteration is not None:
        sample["iteration"] = iteration
    return sample


def _render_transfer_samples(report: dict) -> list[str]:
    lines = [
        "## Raw transfer samples",
        "",
        "| Target | Operation | Iteration | Bytes | Seconds | GB/s | GiB/s | Gbps |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in report["results"]:
        for sample in result.get("raw", {}).get("transfer_samples", []):
            lines.append(
                "| {target} | {operation} | {iteration} | {bytes_count} | {seconds:.6f} | {gb_s:.6f} | {gib_s:.6f} | {gbps:.6f} |".format(
                    target=result["name"],
                    operation=sample["operation"],
                    iteration=sample.get("iteration", ""),
                    bytes_count=sample["bytes"],
                    seconds=sample["seconds"],
                    gb_s=sample["gb_s"],
                    gib_s=sample["gib_s"],
                    gbps=sample["gbps"],
                )
            )
    return lines
