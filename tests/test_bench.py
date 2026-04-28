import json
import tempfile
import unittest
from pathlib import Path

from azure_storage_benchmarks.bench import (
    BenchmarkConfig,
    parse_named_path,
    render_markdown,
    run_benchmarks,
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
                iterations=1,
                run_id="unit",
            )

            report = run_benchmarks({"tmp": Path(tmp)}, config)

        self.assertEqual(report["schema_version"], "3")
        self.assertEqual(report["results"][0]["name"], "tmp")
        self.assertIn("mount", report["results"][0])
        self.assertTrue(report["results"][0]["ok"], report["results"][0]["error"])
        self.assertGreater(report["results"][0]["metrics"]["large_read_mib_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_first_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_warm_gb_s"], 0)
        self.assertGreater(report["results"][0]["metrics"]["large_read_gbps"], 0)
        samples = report["results"][0]["raw"]["transfer_samples"]
        self.assertTrue(any(sample["operation"] == "large_read" for sample in samples))
        self.assertTrue(all("gb_s" in sample for sample in samples))

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
        self.assertIn("Raw transfer samples", markdown)
        self.assertIn("| tmp | large_read | 1 | 1024 |", markdown)


if __name__ == "__main__":
    unittest.main()
