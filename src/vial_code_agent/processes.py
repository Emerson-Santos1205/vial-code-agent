from __future__ import annotations

import os
import signal
import subprocess
from typing import Any


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def terminate_process_tree(
    process: subprocess.Popen[str],
    termination_signal: int = getattr(signal, "SIGKILL", signal.SIGTERM),
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            # type: ignore[attr-defined]
            os.killpg(process.pid, termination_signal)
        except (ProcessLookupError, OSError):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
