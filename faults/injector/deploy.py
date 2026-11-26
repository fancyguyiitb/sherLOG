"""The deploy repo — the target system's "production" git history.

Nimbus services bind-mount their code from ``runtime/deploy-repo`` rather than from this
repository. A deploy is a real commit in that repo followed by a container recreate, and
``git_diff_since`` reads real diffs from it. Keeping it separate means 36 eval runs do not
leave 70 fault/revert commits on the project's own ``master``.

**Leakage guard.** The source in ``nimbus/`` is heavily annotated for human readers —
comments like "this branch is what F-06 deletes". If those reached the deploy repo, any
diff hunk touching them would hand the agent the answer. So the deploy repo holds a
*built* copy: every module is round-tripped through ``ast`` with comments and docstrings
stripped. What the agent can read is code, exactly as a production artifact would be.

Code faults are expressed as edits against that built code (see ``faults/variants``):
replace one method, or replace one string literal. The injector applies the edit, commits
with a plausible author and message, and git produces the diff — no hand-written patch can
drift out of sync with the code it patches.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_REPO = ROOT / "runtime" / "deploy-repo"
ENV_DIR = ROOT / "runtime" / "env"
COMPOSE_FILE = ROOT / "docker-compose.yml"
VARIANTS = ROOT / "faults" / "variants"

BASELINE_TAG = "baseline"
AUTHORS = {
    "fault": "Priya Raman <priya@nimbus.example>",
    "decoy": "Marcus Lee <marcus@nimbus.example>",
    "baseline": "Nimbus Deploy <deploy@nimbus.example>",
}


# --- building production code ----------------------------------------------------------

def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    return tree


def build_source(src: str) -> str:
    """Comments and docstrings out; code unchanged."""
    return ast.unparse(_strip_docstrings(ast.parse(src))) + "\n"


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(DEPLOY_REPO), "-c", "core.autocrlf=false", *args],
        check=check, capture_output=True, text=True,
    ).stdout


def head_sha() -> str:
    return git("rev-parse", "--short=7", "HEAD").strip()


def reset_repo() -> str:
    """(Re)build the deploy repo from ``nimbus/`` and tag the baseline. Idempotent."""
    src_root = ROOT / "nimbus"
    if not (DEPLOY_REPO / ".git").exists():
        DEPLOY_REPO.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(DEPLOY_REPO)], check=True)
    else:
        # Back to the baseline commit, then rewrite the working tree from current source.
        if git("tag", "-l", BASELINE_TAG).strip():
            git("reset", "-q", "--hard", BASELINE_TAG)
        git("clean", "-qfd")
    git("config", "user.name", "Nimbus Deploy")
    git("config", "user.email", "deploy@nimbus.example")

    dest_root = DEPLOY_REPO / "nimbus"
    produced: set[str] = set()
    for path in src_root.rglob("*.py"):
        rel = path.relative_to(src_root)
        if "__pycache__" in rel.parts:
            continue
        out = dest_root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_source(path.read_text(encoding="utf-8")), encoding="utf-8",
                       newline="\n")
        produced.add(("nimbus" / rel).as_posix())

    # Only built code belongs in the deploy repo; anything else tracked is removed.
    stale = [f for f in git("ls-files").splitlines() if f not in produced]
    if stale:
        git("rm", "-q", "--", *stale)

    git("add", "-A")
    if git("status", "--porcelain").strip():
        git("commit", "-q", "-m", "release: nimbus store", "--author", AUTHORS["baseline"])
    git("tag", "-f", BASELINE_TAG)
    return head_sha()


# --- code variants ---------------------------------------------------------------------

def _load_variant(name: str) -> list[dict[str, Any]]:
    path = VARIANTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"variant_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.EDITS)


def _replace_method(text: str, cls: str, method: str, new_src: str) -> str:
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method:
                    lines = text.splitlines(keepends=True)
                    indent = " " * item.col_offset
                    built = build_source(new_src)
                    block = "".join(indent + ln if ln.strip() else ln
                                    for ln in built.splitlines(keepends=True))
                    start = (item.decorator_list[0].lineno if item.decorator_list else item.lineno)
                    return "".join(lines[: start - 1]) + block + "".join(lines[item.end_lineno:])
    raise ValueError(f"{cls}.{method} not found")


def apply_variant(name: str) -> list[str]:
    """Apply a variant's edits to the working tree. Returns the touched paths."""
    touched = []
    for edit in _load_variant(name):
        path = DEPLOY_REPO / edit["file"]
        if edit["op"] == "replace_method":
            text = _replace_method(path.read_text(encoding="utf-8"), edit["class"],
                                   edit["method"], edit["source"])
        elif edit["op"] == "replace_string":
            text = path.read_text(encoding="utf-8")
            if edit["old"] not in text:
                raise ValueError(f"{edit['old']!r} not in {edit['file']}")
            text = text.replace(edit["old"], edit["new"])
        elif edit["op"] == "write_file":
            text = edit["content"]
        else:
            raise ValueError(edit["op"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        touched.append(edit["file"])
    return touched


def commit(message: str, author: str) -> tuple[str, str]:
    git("add", "-A")
    git("commit", "-q", "-m", message, "--author", author)
    return head_sha(), git("show", "--stat", "--format=", "HEAD").strip()


# --- containers ------------------------------------------------------------------------

def recreate(*services: str) -> None:
    """Force-recreate containers with the deploy repo's current HEAD as COMMIT_SHA."""
    env = {**os.environ, "COMMIT_SHA": head_sha()}
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--force-recreate",
         "--no-deps", *services],
        check=True, env=env, capture_output=True,
    )


def write_env(service: str, values: dict[str, object]) -> None:
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    path = ENV_DIR / f"{service}.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def clear_env(service: str) -> None:
    write_env(service, {})
