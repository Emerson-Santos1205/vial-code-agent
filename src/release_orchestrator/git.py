from __future__ import annotations

from pathlib import Path
import subprocess

from .domain import Commit, TAG_PREFIX, release_tag


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def is_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(root: Path) -> str:
    result = run_git(root, "branch", "--show-current", check=False)
    branch = result.stdout.strip()
    if branch:
        return branch
    head = run_git(root, "rev-parse", "--short", "HEAD", check=False)
    sha = head.stdout.strip()
    return f"HEAD ({sha})" if sha else "HEAD"


def last_commit(root: Path) -> str:
    result = run_git(root, "log", "-1", "--pretty=format:%h %s", check=False)
    text = result.stdout.strip()
    return text or "no commits yet"


def modified_files(root: Path) -> list[str]:
    result = run_git(root, "status", "--porcelain=v1", check=False)
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def has_dirty_tree(root: Path) -> bool:
    return bool(modified_files(root))


def tag_exists(root: Path, version_or_tag: str) -> bool:
    tag = version_or_tag if version_or_tag.startswith(TAG_PREFIX) else release_tag(version_or_tag)
    result = run_git(root, "tag", "--list", tag, check=False)
    return tag in {line.strip() for line in result.stdout.splitlines()}


def latest_tag(root: Path) -> str | None:
    result = run_git(root, "tag", "--list", f"{TAG_PREFIX}*", "--sort=-version:refname", check=False)
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


def commits_since(root: Path, since_tag: str | None) -> list[Commit]:
    args = ["log", "--pretty=format:%H%x00%s"]
    if since_tag:
        args.append(f"{since_tag}..HEAD")
    result = run_git(root, *args, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    commits: list[Commit] = []
    for line in reversed(lines):
        sha, subject = line.split("\x00", 1)
        commits.append(Commit(sha=sha, subject=subject))
    return commits


def create_annotated_tag(root: Path, tag: str, message: str) -> None:
    run_git(root, "tag", "-a", tag, "-m", message)


def delete_tag(root: Path, tag: str) -> None:
    run_git(root, "tag", "-d", tag)


def get_submodule_commit(root: Path, relative_path: str = "vendor/vial-core") -> str:
    """Retorna o commit SHA atualmente fixado do submódulo."""
    sub_dir = root / relative_path
    if not sub_dir.is_dir():
        return ""
    result = run_git(sub_dir, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def get_submodule_remote_commit(root: Path, relative_path: str = "vendor/vial-core") -> str:
    """Obtém o commit SHA mais recente do remoto configurado no submódulo."""
    sub_dir = root / relative_path
    if not sub_dir.is_dir():
        return ""
    # Busca a referência remota (default origin/main ou origin/master)
    result = run_git(sub_dir, "ls-remote", "origin", "HEAD", check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split()[0]
    return ""


def check_submodule_drift(root: Path, relative_path: str = "vendor/vial-core") -> dict[str, str | int | bool]:
    """Valida o estado e desfasamento (drift) do submódulo."""
    sub_dir = root / relative_path
    if not sub_dir.is_dir():
        return {"exists": False, "dirty": False, "current_sha": "", "remote_sha": "", "lag_count": -1}

    current_sha = get_submodule_commit(root, relative_path)
    dirty = has_dirty_tree(sub_dir)

    # Tenta buscar do remoto com timeout implícito/suave
    remote_sha = get_submodule_remote_commit(root, relative_path)
    lag_count = 0

    if current_sha and remote_sha and current_sha != remote_sha:
        # Tenta calcular quantos commits o submódulo local está atrás da remote_sha se disponível
        run_git(sub_dir, "fetch", "origin", check=False)
        rev_list = run_git(sub_dir, "rev-list", "--count", f"{current_sha}..origin/main", check=False)
        if rev_list.returncode == 0 and rev_list.stdout.strip().isdigit():
            lag_count = int(rev_list.stdout.strip())
        else:
            lag_count = -1

    return {
        "exists": True,
        "dirty": dirty,
        "current_sha": current_sha,
        "remote_sha": remote_sha,
        "lag_count": lag_count,
        "synced": (current_sha == remote_sha) if (current_sha and remote_sha) else True,
    }


def update_submodule(root: Path, relative_path: str = "vendor/vial-core") -> bool:
    """Atualiza a referência do submódulo para o commit remoto mais recente."""
    sub_dir = root / relative_path
    if not sub_dir.is_dir():
        return False
    res_fetch = run_git(sub_dir, "fetch", "origin", check=False)
    if res_fetch.returncode != 0:
        return False
    res_pull = run_git(sub_dir, "checkout", "origin/main", check=False)
    if res_pull.returncode != 0:
        res_pull = run_git(sub_dir, "checkout", "origin/master", check=False)
    return res_pull.returncode == 0

