"""开发环境自检的测试。

只断言**结构与语义**，不断言具体环境：
换台机器、CI 上没有 FFmpeg 或 Ollama 都是正常的，不应该导致测试失败。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core import diagnostics  # noqa: E402
from xbc.core.context import AppContext  # noqa: E402
from xbc.core.diagnostics import OPTIONAL, REQUIRED, exit_code, run_checks  # noqa: E402


class DiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-diag-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)

    def test_report_has_expected_shape(self) -> None:
        report = run_checks(self.ctx)
        self.assertIn(report["status"], {"ok", "degraded", "broken"})
        for name in ("python", "data_dir", "plugin_paths", "gui", "git", "ffmpeg", "ai"):
            self.assertIn(name, report["checks"], f"缺少检查项 {name}")
        self.assertEqual(report["checks"]["python"]["level"], REQUIRED)

    def test_python_check_describes_current_interpreter(self) -> None:
        report = run_checks(self.ctx)
        self.assertEqual(report["checks"]["python"]["executable"], sys.executable)
        self.assertTrue(report["checks"]["python"]["ok"])

    def test_data_dir_check_leaves_no_probe_file(self) -> None:
        report = run_checks(self.ctx)
        self.assertTrue(report["checks"]["data_dir"]["ok"])
        self.assertFalse((self.root / ".write-probe").exists(), "探针文件没有被清理")

    def test_required_failure_reports_broken_and_nonzero_exit(self) -> None:
        failing = {"ok": False, "level": REQUIRED, "reason": "模拟：数据目录不可写"}
        with mock.patch.object(diagnostics, "check_data_dir", return_value=failing):
            report = diagnostics.run_checks(self.ctx)
        self.assertEqual(report["status"], "broken")
        self.assertIn("data_dir", report["broken"])
        self.assertEqual(exit_code(report), 1)

    def test_optional_failure_only_degrades(self) -> None:
        failing = {"ok": False, "level": OPTIONAL, "reason": "模拟：本地模型未启动"}
        with mock.patch.object(diagnostics, "check_ai", return_value=failing):
            report = diagnostics.run_checks(self.ctx)
        self.assertNotEqual(report["status"], "broken", "可选能力缺失不应判定环境不可用")
        self.assertEqual(exit_code(report), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
