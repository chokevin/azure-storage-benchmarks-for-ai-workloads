from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import statistics
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MIB = 1024 * 1024
KIB = 1024


@dataclass(frozen=True)
class BenchmarkConfig:
    small_files: int = 1000
    small_file_size_kib: int = 4
    large_file_size_mib: int = 256
    checkpoint_files: int = 4
    checkpoint_size_mib: int = 64
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
        "schema_version": "1",
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
        small_dir.mkdir()
        large_dir.mkdir()
        checkpoint_dir.mkdir()

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

        small_total_bytes = config.small_files * config.small_file_size_kib * KIB
        large_total_bytes = config.large_file_size_mib * MIB
        checkpoint_total_bytes = config.checkpoint_files * config.checkpoint_size_mib * MIB

        metrics = {
            "small_write_ms_per_file": _ms_per_item(raw["small_write_seconds"], config.small_files),
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
            "large_read_mib_s": _mib_per_second(
                large_total_bytes, statistics.median(raw["large_read_seconds"])
            ),
            "checkpoint_write_mib_s": _mib_per_second(
                checkpoint_total_bytes, raw["checkpoint_write_seconds"]
            ),
        }

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
        "| Target | OK | Small read ms/file | List ms/1000 files | Large read MiB/s | Large write MiB/s | Checkpoint write MiB/s | Error |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in report["results"]:
        metrics = result.get("metrics", {})
        lines.append(
            "| {name} | {ok} | {small_read} | {list_ms} | {large_read} | {large_write} | {checkpoint_write} | {error} |".format(
                name=result["name"],
                ok="yes" if result["ok"] else "no",
                small_read=_fmt_metric(metrics.get("small_read_ms_per_file")),
                list_ms=_fmt_metric(metrics.get("list_ms_per_1000_files")),
                large_read=_fmt_metric(metrics.get("large_read_mib_s")),
                large_write=_fmt_metric(metrics.get("large_write_mib_s")),
                checkpoint_write=_fmt_metric(metrics.get("checkpoint_write_mib_s")),
                error=(result.get("error") or "").replace("|", "\\|"),
            )
        )
    lines.append("")
    lines.append("Lower latency is better for `ms/*` columns. Higher throughput is better for `MiB/s` columns.")
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

