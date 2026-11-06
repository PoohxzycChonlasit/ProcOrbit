from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_PREVIOUS_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
_TEST_LOCALAPPDATA = Path(tempfile.mkdtemp(prefix=".test-runtime-", dir=ROOT))
os.environ["LOCALAPPDATA"] = str(_TEST_LOCALAPPDATA)
sys.path.insert(0, str(ROOT / "app"))


def _remove_test_runtime() -> None:
    shutil.rmtree(_TEST_LOCALAPPDATA, ignore_errors=True)
    if _PREVIOUS_LOCALAPPDATA is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _PREVIOUS_LOCALAPPDATA


atexit.register(_remove_test_runtime)

from health_score import score_run_health  # noqa: E402
from monitor import script_info  # noqa: E402


class ScriptInfoTests(unittest.TestCase):
    def test_module_invocation_is_labeled_without_a_script_path(self) -> None:
        label, script_path, module, project = script_info(
            ["python.exe", "-m", "http.server", "8000"],
            "python.exe",
            r"C:\work",
        )

        self.assertEqual(label, "http.server")
        self.assertIsNone(script_path)
        self.assertEqual(module, "http.server")
        self.assertEqual(project, r"C:\work")

    def test_relative_script_is_resolved_against_working_directory(self) -> None:
        cwd = str(ROOT / "sample-project")
        label, script_path, module, project = script_info(
            ["python.exe", "jobs/worker.py"], "python.exe", cwd
        )

        self.assertEqual(label, "worker.py")
        self.assertEqual(
            script_path,
            str((Path(cwd) / "jobs" / "worker.py").resolve(strict=False)),
        )
        self.assertIsNone(module)
        self.assertEqual(project, cwd)

    def test_python_command_is_distinguished_from_script(self) -> None:
        label, script_path, module, project = script_info(
            ["python.exe", "-c", "print(1)"], "python.exe", r"C:\work"
        )

        self.assertEqual(label, "python.exe -c")
        self.assertIsNone(script_path)
        self.assertIsNone(module)
        self.assertEqual(project, r"C:\work")

    def test_missing_command_line_is_marked_unavailable(self) -> None:
        self.assertEqual(
            script_info(None, "python.exe", None),
            ("python.exe (command unavailable)", None, None, None),
        )

    def test_interactive_interpreter_is_labeled(self) -> None:
        label, script_path, module, project = script_info(
            ["python.exe"], "python.exe", None
        )

        self.assertEqual(label, "Interactive Python")
        self.assertIsNone(script_path)
        self.assertIsNone(module)
        self.assertIsNone(project)


class HealthScoreEvidenceTests(unittest.TestCase):
    def test_no_samples_produce_no_score_and_low_confidence(self) -> None:
        result = score_run_health({}, [])

        self.assertIsNone(result["score"])
        self.assertIsNone(result["grade"])
        self.assertEqual(result["confidence"], "LOW")
        self.assertTrue(
            any("Not enough measured data" in item for item in result["explanations"])
        )


if __name__ == "__main__":
    unittest.main()
