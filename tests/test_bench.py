import json
import tempfile
import unittest
from pathlib import Path

from azure_storage_benchmarks.bench import (
    AmlfsValidationConfig,
    BenchmarkConfig,
    DatasetReadConfig,
    render_amlfs_validation_markdown,
    parse_named_path,
    render_dataset_read_markdown,
    render_markdown,
    run_amlfs_validation,
    run_benchmarks,
    run_dataset_read_benchmarks,
    write_json_report,
)


class BenchmarkTests(unittest.TestCase):
    def test_parse_named_path(self):
        name, path = parse_named_path("nfs=/data-nfs")

        self.assertEqual(name, "nfs")
        self.assertEqual(path, Path("/data-nfs"))

    def test_parse_named_path_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            parse_named_path("/data-nfs")
        with self.assertRaises(ValueError):
            parse_named_path("=/data-nfs")
        with self.assertRaises(ValueError):
            parse_named_path("nfs=")

    def test_run_benchmarks_on_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BenchmarkConfig(
                small_files=3,
                small_file_size_kib=1,
                large_file_size_mib=1,
                checkpoint_files=1,
                checkpoint_size_mib=1,
                async_checkpoint=True,
                iterations=1,
                run_id="unit",
            )

            report = run_benchmarks({"tmp": Path(tmp)}, config)

        self.assertEqual(report["schema_version"], "4")
        self.assertEqual(report["results"][0]["name"], "tmp")
        self.assertIn("mount", report["results"][0])
        self.assertTrue(report["results"][0]["ok"], report["results"][0]["error"])
        self.assertGreater(report["results"][0]["metrics"]["large_read_mib_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_first_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_warm_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_gbps"], 0)
        self.assertGreater(report["results"][0]["metrics"]["async_checkpoint_writer_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["async_checkpoint_blocked_ms"], 0)
        samples = report["results"][0]["raw"]["transfer_samples"]
        self.assertTrue(any(sample["operation"] == "large_read" for sample in samples))
        self.assertTrue(any(sample["operation"] == "async_checkpoint_writer" for sample in samples))
        self.assertTrue(any(sample["operation"] == "async_checkpoint_blocked" for sample in samples))
        self.assertTrue(all("gb_s" in sample for sample in samples))

    def test_run_dataset_read_benchmarks_caps_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.bin").write_bytes(b"b" * 8)
            (root / "a.bin").write_bytes(b"a" * 8)
            config = DatasetReadConfig(max_bytes=12, concurrency=2, block_size_mib=1, run_id="dataset")

            report = run_dataset_read_benchmarks({"tmp": root}, config)

        result = report["results"][0]
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(report["schema_version"], "1")
        self.assertEqual(result["metrics"]["bytes_read"], 12)
        self.assertEqual(result["metrics"]["files_read"], 2)
        self.assertEqual(result["metrics"]["files_fully_read"], 1)
        self.assertEqual(
            [entry["path"] for entry in result["raw"]["selected_files"]],
            ["a.bin", "b.bin"],
        )
        self.assertEqual(
            [entry["bytes_to_read"] for entry in result["raw"]["selected_files"]],
            [8, 4],
        )
        self.assertGreater(result["raw"]["transfer_samples"][0]["gb_s"], 0)

    def test_run_dataset_read_benchmarks_allows_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = DatasetReadConfig(max_bytes=None, concurrency=4, block_size_mib=1, run_id="empty")

            report = run_dataset_read_benchmarks({"tmp": Path(tmp)}, config)

        result = report["results"][0]
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["metrics"]["bytes_read"], 0)
        self.assertEqual(result["metrics"]["files_read"], 0)
        self.assertEqual(result["metrics"]["read_gb_s"], 0.0)
        self.assertEqual(result["raw"]["selected_files"], [])

    def test_render_dataset_read_markdown(self):
        report = {
            "run_id": "dataset",
            "started_at": "2026-01-01T00:00:00Z",
            "host": {"hostname": "test"},
            "config": {"max_bytes": 1024, "concurrency": 4, "block_size_mib": 8},
            "results": [
                {
                    "name": "lustre",
                    "ok": True,
                    "mount": {"fs_type": "lustre", "source": "10.0.0.1@tcp:/fs"},
                    "metrics": {
                        "files_read": 2,
                        "files_fully_read": 2,
                        "bytes_read": 1024,
                        "enumerate_seconds": 0.1,
                        "read_seconds": 0.2,
                        "wall_seconds": 0.3,
                        "read_gb_s": 0.001,
                        "read_gib_s": 0.001,
                        "read_gbps": 0.008,
                    },
                    "raw": {
                        "selected_bytes": 1024,
                        "size_histogram": {
                            "0_64k": 2,
                            "64k_1m": 0,
                            "1m_64m": 0,
                            "64m_1g": 0,
                            "1g_plus": 0,
                        },
                    },
                    "error": "",
                }
            ],
        }

        markdown = render_dataset_read_markdown(report)

        self.assertIn("Dataset Read Benchmark Results", markdown)
        self.assertIn("| lustre | yes | lustre | 10.0.0.1@tcp:/fs |", markdown)
        self.assertIn("Dataset details", markdown)

    def test_run_amlfs_validation_samples_dataset_without_lfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b.bin").write_bytes(b"b" * 8)
            (root / "a.bin").write_bytes(b"a" * 4)
            config = AmlfsValidationConfig(sample_files=1, run_id="amlfs")

            report = run_amlfs_validation({"amlfs": root}, config)

        result = report["results"][0]
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(report["schema_version"], "1")
        self.assertEqual(result["dataset"]["sample_files"], 1)
        self.assertEqual(result["dataset"]["sample_bytes"], 4)
        self.assertFalse(result["hsm"]["lfs_available"])
        self.assertIn("lfs command is not available", result["hsm"]["state"]["stderr"])

    def test_render_amlfs_validation_markdown(self):
        report = {
            "run_id": "amlfs",
            "started_at": "2026-01-01T00:00:00Z",
            "host": {"hostname": "test"},
            "config": {"sample_files": 10},
            "results": [
                {
                    "name": "amlfs",
                    "ok": True,
                    "mount": {"fs_type": "lustre", "source": "10.0.0.1@tcp:/lustrefs"},
                    "dataset": {"sample_files": 2, "sample_bytes": 1024},
                    "hsm": {
                        "lfs_available": True,
                        "state": {"returncode": 0, "stdout": "released exists", "stderr": ""},
                    },
                    "error": "",
                }
            ],
        }

        markdown = render_amlfs_validation_markdown(report)

        self.assertIn("AMLFS Strategy Validation", markdown)
        self.assertIn("| amlfs | yes | lustre | 10.0.0.1@tcp:/lustrefs |", markdown)
        self.assertIn("| yes | yes |", markdown)

    def test_write_json_and_render_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {
                "run_id": "unit",
                "started_at": "2026-01-01T00:00:00Z",
                "host": {"hostname": "test"},
                "results": [
                    {
                        "name": "tmp",
                        "ok": True,
                        "mount": {"fs_type": "tmpfs", "source": "tmpfs"},
                        "metrics": {
                            "small_read_first_ms_per_file": 1.0,
                            "small_read_warm_ms_per_file": 0.5,
                            "list_ms_per_1000_files": 2.0,
                            "large_read_first_gb_s": 3.0,
                            "large_read_warm_gb_s": 6.0,
                            "large_write_gb_s": 4.0,
                            "checkpoint_write_gb_s": 5.0,
                            "async_checkpoint_submit_ms": 0.1,
                            "async_checkpoint_wait_ms": 10.0,
                            "async_checkpoint_blocked_gb_s": 7.0,
                            "async_checkpoint_writer_gb_s": 4.5,
                        },
                        "raw": {
                            "transfer_samples": [
                                {
                                    "operation": "large_read",
                                    "iteration": 1,
                                    "bytes": 1024,
                                    "seconds": 0.001,
                                    "gb_s": 0.001024,
                                    "gib_s": 0.000954,
                                    "gbps": 0.008192,
                                }
                            ]
                        },
                        "error": "",
                    }
                ],
            }
            output = Path(tmp) / "report.json"

            write_json_report(report, output)
            loaded = json.loads(output.read_text(encoding="utf-8"))
            markdown = render_markdown(loaded)

        self.assertEqual(loaded["run_id"], "unit")
        self.assertIn("| tmp | yes | tmpfs | tmpfs |", markdown)
        self.assertIn("Large read first GB/s", markdown)
        self.assertIn("Async blocked GB/s", markdown)
        self.assertIn("Raw transfer samples", markdown)
        self.assertIn("| tmp | large_read | 1 | 1024 |", markdown)


if __name__ == "__main__":
    unittest.main()
