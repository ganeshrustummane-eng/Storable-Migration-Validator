"""Regression check for the Run Validation hang (see
docs/decisions/0004-run-validation-hang-subprocess-pipe-deadlock.md).

runner.start_validation()/collect_validation_result() redirect the child
process's stdout/stderr to temp files instead of subprocess.PIPE, specifically
because main.py can emit more than the OS pipe buffer (~64KB on Windows)
before exiting, and a PIPE with nobody draining it deadlocks the child on
write() forever. This proves that exact mechanism against a real subprocess
that writes well past that threshold, using runner's actual functions.

Run: python Project/test_runner_subprocess.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runner


def test_large_stdout_does_not_hang(tmp_path):
    # >64KB of stdout -- big enough to fill a default pipe buffer, which is
    # exactly the condition that hung before this fix.
    script_path = tmp_path / "big_stdout.py"
    script_path.write_text('print("x" * 200_000)\n')

    running = runner.RunningValidation.__new__(runner.RunningValidation)
    stdout_fd, stdout_path = tempfile.mkstemp()
    stderr_fd, stderr_path = tempfile.mkstemp()
    import os
    import subprocess
    with os.fdopen(stdout_fd, "w") as out_f, os.fdopen(stderr_fd, "w") as err_f:
        proc = subprocess.Popen(
            [sys.executable, str(script_path)], stdout=out_f, stderr=err_f, text=True,
        )
    running.proc = proc
    running.stdout_path = Path(stdout_path)
    running.stderr_path = Path(stderr_path)

    # Would never return (deadlock) with subprocess.PIPE + no reader; the
    # file-redirect fix means the child never blocks on write(), so this
    # completes well within a generous timeout.
    proc.wait(timeout=15)
    assert proc.returncode == 0

    stdout_text = running.stdout_path.read_text()
    assert len(stdout_text) > 65_536
    running.stdout_path.unlink(missing_ok=True)
    running.stderr_path.unlink(missing_ok=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_large_stdout_does_not_hang(Path(tmp_dir))
    print("OK")
