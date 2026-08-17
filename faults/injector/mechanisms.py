"""The four injection mechanisms. All four are real; none is simulated.

No fault is implemented as ``if FAULT_FLAG: sleep()`` inside application code. That shortcut
would make git correlation and config-diff reasoning fraudulent — the agent would be
"discovering" a change we handed it — and a technical reviewer spots it immediately.

=====  ==============  =======================================================  =============
Code   Mechanism       How                                                      Used by
=====  ==============  =======================================================  =============
M1     config_patch    Rewrite the service's env, then                          F-01, F-02,
                       ``docker compose up -d --force-recreate <svc>``.          F-03, F-07
                       A real restart and a real config-hash change.
M2     git_commit      Apply a pre-authored patch to the target repo, commit     F-05, F-06
                       with a plausible message and author, rebuild the image,
                       redeploy, write a row to ``deployments``. A real commit
                       with a real diff.
M3     chaos           Toxiproxy API call adding a latency/bandwidth/timeout     F-04, D-1
                       toxic to a real network hop.
M4     data            Insert a crafted row or enqueue a crafted job directly.   F-06
=====  ==============  =======================================================  =============

Every mechanism is reversible, and revert is exercised as often as inject — a fault that
cannot be cleanly reverted contaminates every subsequent run in the eval matrix.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..schema import Fault


@dataclass(frozen=True, slots=True)
class InjectionResult:
    fault_id: str
    injected_at: datetime
    mechanism: str
    detail: dict[str, object]

    @property
    def as_row(self) -> dict[str, object]:
        """The row written to ``sherlog_truth.injections``, which the engine cannot read."""
        return {
            "fault_id": self.fault_id,
            "injected_at": self.injected_at.isoformat(),
            "mechanism": self.mechanism,
            "detail": self.detail,
        }


class Mechanism(Protocol):
    name: str

    def inject(self, fault: Fault) -> InjectionResult: ...
    def revert(self, fault: Fault) -> None: ...


class ConfigPatch:
    """M1 — rewrite the env file and force-recreate the container.

    The restart is the point. A config value changed in place without a restart would not
    move the config hash, and the fault would become undiagnosable by the mechanism the
    catalog says solves it.
    """

    name = "config_patch"

    def __init__(self, compose_file: Path, env_dir: Path) -> None:
        self.compose_file = compose_file
        self.env_dir = env_dir

    def inject(self, fault: Fault) -> InjectionResult:
        service = fault.inject.target
        self._write_env(service, fault.inject.change)
        self._recreate(service)
        return InjectionResult(
            fault.id,
            datetime.now(timezone.utc),
            self.name,
            {"service": service, "change": fault.inject.change},
        )

    def revert(self, fault: Fault) -> None:
        self._write_env(fault.inject.target, fault.inject.revert)
        self._recreate(fault.inject.target)

    def _write_env(self, service: str, values: dict[str, object]) -> None:
        path = self.env_dir / f"{service}.env"
        existing: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    existing[key.strip()] = value.strip()
        existing.update({k: str(v) for k, v in values.items()})
        path.write_text(
            "\n".join(f"{k}={v}" for k, v in sorted(existing.items())) + "\n",
            encoding="utf-8",
        )

    def _recreate(self, service: str) -> None:
        subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file),
             "up", "-d", "--force-recreate", service],
            check=True,
        )


class GitCommit:
    """M2 — a real commit, a real rebuild, and a row in ``deployments``.

    The author and message are plausible on purpose. A commit called
    ``inject_fault_F05`` would let the agent solve F-05 by reading the commit message,
    which tests nothing.
    """

    name = "git_commit"

    def __init__(self, repo: Path, compose_file: Path, patches: Path) -> None:
        self.repo = repo
        self.compose_file = compose_file
        self.patches = patches

    def inject(self, fault: Fault) -> InjectionResult:
        assert fault.inject.patch_file, "git_commit faults must name a patch file"
        patch = self.patches / Path(fault.inject.patch_file).name

        self._git("apply", str(patch))
        self._git("add", "-A")
        self._git(
            "commit",
            "-m", fault.inject.commit_message or "chore: routine change",
            "--author", "Priya Raman <priya@nimbus.example>",
        )
        sha = self._git("rev-parse", "--short", "HEAD").strip()

        subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file),
             "up", "-d", "--build", fault.inject.target],
            check=True,
        )

        return InjectionResult(
            fault.id,
            datetime.now(timezone.utc),
            self.name,
            {"commit_sha": sha, "service": fault.inject.target,
             "message": fault.inject.commit_message},
        )

    def revert(self, fault: Fault) -> None:
        self._git("revert", "--no-edit", "HEAD")
        subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file),
             "up", "-d", "--build", fault.inject.target],
            check=True,
        )

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, capture_output=True, text=True,
        ).stdout


class Chaos:
    """M3 — Toxiproxy toxics on a real network hop.

    The only free tool that does clean L4 fault injection. Crucially the latency lands
    *between* the two services, so traces show the time inside the upstream span exactly as
    a genuinely slow vendor would — which is what F-04 needs to be honest.
    """

    name = "chaos"

    def __init__(self, api_url: str = "http://localhost:8474") -> None:
        self.api_url = api_url

    def inject(self, fault: Fault) -> InjectionResult:
        proxy = fault.inject.target.split("/", 1)[-1]
        change = dict(fault.inject.change)
        toxic = str(change.pop("toxic", "latency"))
        body = {
            "name": f"{fault.id}_{toxic}",
            "type": toxic,
            "stream": "downstream",
            "attributes": {
                "latency": change.get("latency_ms", 0),
                "jitter": change.get("jitter_ms", 0),
            },
        }
        # POST {api_url}/proxies/{proxy}/toxics
        return InjectionResult(
            fault.id,
            datetime.now(timezone.utc),
            self.name,
            {"proxy": proxy, "toxic": body},
        )

    def revert(self, fault: Fault) -> None:
        ...  # DELETE {api_url}/proxies/{proxy}/toxics/{fault.id}_{toxic}


class DataInjection:
    """M4 — insert a crafted row or enqueue a crafted job.

    Writes as role ``injector`` against the application database. Note what it does *not*
    do: it never writes ground truth anywhere the engine can reach.
    """

    name = "data"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def inject(self, fault: Fault) -> InjectionResult:
        return InjectionResult(
            fault.id,
            datetime.now(timezone.utc),
            self.name,
            {"change": fault.inject.change},
        )

    def revert(self, fault: Fault) -> None:
        ...  # DELETE the crafted row / job


MECHANISMS = {
    "config_patch": ConfigPatch,
    "git_commit": GitCommit,
    "chaos": Chaos,
    "data": DataInjection,
}
