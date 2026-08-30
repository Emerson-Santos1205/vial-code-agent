"""Run a minimal OpenCode request to validate a provider before benchmarking."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _event_summary(stdout: str) -> tuple[str, list[dict[str, str]]]:
    text: list[str] = []
    errors: list[dict[str, str]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            error = event.get("error")
            if isinstance(error, dict):
                errors.append({
                    "name": str(error.get("name", "error")),
                    "message": str(error.get("message", ""))[:500],
                })
            else:
                errors.append({"name": "error", "message": str(error)[:500]})
        part = event.get("part")
        if event.get("type") == "text" and isinstance(part, dict):
            value = part.get("text")
            if isinstance(value, str):
                text.append(value)
    return "".join(text), errors


def check_provider(model: str, image: str, auth: Path, docker: str = "docker",
                   timeout_seconds: int = 120) -> dict[str, Any]:
    """Return a sanitized health result and never expose credentials."""
    command = [
        docker, "run", "--rm",
        "--mount", f"type=bind,src={auth.resolve().as_posix()},"
                    "dst=/root/.local/share/opencode/auth.json,readonly",
        "--entrypoint", "opencode", image, "run", "--pure",
        "--format", "json", "--model", model,
        "Reply with exactly PONG.",
    ]
    result: dict[str, Any] = {
        "model": model,
        "image": image,
        "returncode": None,
        "response_received": False,
        "error_events": [],
    }
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_seconds, check=False)
    except FileNotFoundError:
        result.update({"status": "unhealthy", "error": "docker_not_found"})
        return result
    except subprocess.TimeoutExpired:
        result.update({"status": "unhealthy", "error": "timeout"})
        return result
    result["returncode"] = process.returncode
    text, errors = _event_summary(process.stdout)
    result["response_received"] = bool(text.strip())
    result["error_events"] = errors
    if process.returncode != 0:
        result["error"] = "process_failed"
    elif errors:
        result["error"] = "provider_error"
    elif not text.strip():
        result["error"] = "empty_response"
    else:
        result["status"] = "healthy"
        return result
    result["status"] = "unhealthy"
    stderr = process.stderr.strip()
    if stderr:
        result["stderr"] = stderr[-1000:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--auth", type=Path, default=Path.home() / ".local" /
                        "share/opencode/auth.json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.auth.is_file():
        result = {"model": args.model, "image": args.image, "status": "unhealthy",
                  "error": "credentials_not_found"}
    else:
        result = check_provider(args.model, args.image, args.auth)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
