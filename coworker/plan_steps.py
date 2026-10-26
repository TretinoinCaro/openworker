"""The steps of a plan the user approved in auto-approve, and exact matching (OPE-237).

In auto-approve the agent can propose a plan as a list of steps. Once the user approves
it, every later action in the same request is checked here against the steps, by code.
Only an exact match counts:

* ``write``  - a file tool writing exactly that file (every file, for a patch);
* ``delete`` - a plain delete of exactly that file (``rm``/``del``/``Remove-Item`` with no
  pattern, no recursion, nothing chained), or a patch's ``Delete File`` of it;
* ``run``    - exactly that command, ignoring spacing;
* ``send``   - that file going to exactly that host (a network command or a web tool
  whose every URL is that host), or a connector action to exactly that recipient.

The reviewer then gets one fixed line saying whether the action is a step
(``review_facts.PLAN_STEP`` / ``PLAN_NOT_STEP``). It never sees the plan's text: the
steps are written by the agent, and a step can only ever vouch for itself.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .review_facts import _URL_HOST, shell_reaches_network

KINDS = ("write", "delete", "run", "send")
MAX_STEPS = 30
MAX_TEXT = 1000

_FILE_TOOLS = {"write_file", "replace_in_file", "apply_patch", "apply_unified_diff"}
_PATCH_DELETE = re.compile(r"^\*\*\* Delete File: (.+)$", re.MULTILINE)
# Anything that makes a command more than "delete these named files": chaining, pipes,
# redirection, substitution, globbing, variables.
_SHELL_META = re.compile(r"[;&|`$<>(){}*?\[\]\n\r]")
_DELETE_COMMANDS = {"rm", "del", "erase", "remove-item", "ri"}
_DELETE_FLAGS = {"-f", "--force", "-force", "-path", "-literalpath", "/q", "/f"}
_LOOKS_LIKE_FILE = re.compile(r"^[\w.-]+\.[A-Za-z0-9]{1,8}$")


@dataclass(frozen=True)
class PlanStep:
    kind: str
    target: str
    destination: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target, **({"destination": self.destination} if self.destination else {})}


def normalize_steps(raw: Any) -> tuple[list[PlanStep], int]:
    """The well-formed steps, and how many entries were dropped."""
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    steps: list[PlanStep] = []
    dropped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        target = str(entry.get("target") or "").strip()
        destination = str(entry.get("destination") or "").strip().lower() if kind == "send" else ""
        if (
            kind not in KINDS
            or not target
            or len(target) > MAX_TEXT
            or len(destination) > MAX_TEXT
            or (kind == "send" and not destination)
            or len(steps) >= MAX_STEPS
        ):
            dropped += 1
            continue
        steps.append(PlanStep(kind, target, destination))
    return steps, dropped


def _words(command: str) -> list[str]:
    try:
        return [w.strip("'\"") for w in shlex.split(command, posix=False)]
    except ValueError:
        return command.split()


def step_is_risky(step: PlanStep) -> bool:
    """Shown as risky on the card: sending, deleting, pushing, or a command that deletes or
    reaches the network. Writes outside the workspace are marked by the engine, which
    knows the workspace."""
    if step.kind in ("delete", "send"):
        return True
    if step.kind == "run":
        words = [w.lower() for w in _words(step.target)]
        if not words:
            return False
        if shell_reaches_network(step.target):
            return True
        if any(w in _DELETE_COMMANDS or w in ("rmdir", "rd") for w in words):
            return True
        return any(words[i] == "git" and "push" in words[i + 1 :] for i in range(len(words)))
    return False


def _key(path: Path) -> str:
    text = path.as_posix()
    return text.casefold() if sys.platform in ("win32", "darwin") else text


def _host(host: str) -> str:
    host = host.lower().strip().strip(".")
    return host[4:] if host.startswith("www.") else host


def _norm_command(command: Any) -> str:
    return " ".join(str(command or "").split())


def _plain_delete_paths(command: str) -> Optional[list[str]]:
    """The files a command deletes when it is only a plain delete of named files."""
    if _SHELL_META.search(command):
        return None
    words = _words(command)
    if not words or words[0].lower() not in _DELETE_COMMANDS:
        return None
    paths = []
    for word in words[1:]:
        if word.lower() in _DELETE_FLAGS:
            continue
        if word.startswith("-"):
            return None  # -r, -rf, -Recurse …: not a plain file delete
        paths.append(word)
    return paths or None


def _mentions(text: str, name: str) -> bool:
    base = Path(name.replace("\\", "/")).name or name
    return re.search(r"(?<![\w.-])" + re.escape(base) + r"(?![\w.-])", text) is not None


def _covered(paths: Iterable[str], approved: set[str], resolve: Callable[[str], Path]) -> bool:
    keys = [_key(resolve(p)) for p in paths]
    return bool(keys) and all(k in approved for k in keys)


def match_step(
    steps: list[PlanStep],
    tool_name: str,
    arguments: dict[str, Any],
    *,
    resolve: Callable[[str], Path],
    egress_host: Optional[Callable[[str, dict[str, Any]], str]] = None,
) -> bool:
    """True only when the action is exactly one of the approved steps."""
    if not steps:
        return False
    args = arguments if isinstance(arguments, dict) else {}
    try:
        writes = {_key(resolve(s.target)) for s in steps if s.kind == "write"}
        deletes = {_key(resolve(s.target)) for s in steps if s.kind == "delete"}

        if tool_name in _FILE_TOOLS:
            from .permissions import write_paths

            paths, located = write_paths(tool_name, args)
            if not located:
                return False
            removed = _PATCH_DELETE.findall(str(args.get("patch", ""))) if tool_name == "apply_patch" else []
            removed = [p.strip() for p in removed]
            changed = [p for p in paths if p not in removed]
            return (not changed or _covered(changed, writes, resolve)) and (
                not removed or _covered(removed, deletes, resolve)
            ) and bool(changed or removed)

        if tool_name == "run_shell":
            command = str(args.get("command", ""))
            if any(s.kind == "run" and _norm_command(s.target) == _norm_command(command) for s in steps):
                return True
            deleted = _plain_delete_paths(command)
            if deleted is not None and _covered(deleted, deletes, resolve):
                return True
            if shell_reaches_network(command):
                hosts = {_host(m.group(1)) for m in _URL_HOST.finditer(command)}
                return bool(hosts) and any(
                    s.kind == "send" and hosts == {_host(s.destination)} and _mentions(command, s.target)
                    for s in steps
                )
            return False

        sends = [s for s in steps if s.kind == "send"]
        if not sends:
            return False
        blob = json.dumps(args, ensure_ascii=False, default=str)
        if args.get("url") and egress_host is not None:
            # A web tool: its URL's host, exactly.
            host = _host(egress_host(tool_name, args) or "")
            return bool(host) and any(_host(s.destination) == host and _mentions(blob, s.target) for s in sends)

        from .connectors.tool_defs import standing_target_for

        recipient = str(standing_target_for(tool_name, args) or "").strip().lower()
        if not recipient:
            return False  # e.g. a browser upload: where it goes is not in the action
        return any(
            s.destination.strip().lower() == recipient
            and (not _LOOKS_LIKE_FILE.match(s.target) or _mentions(blob, s.target))
            for s in sends
        )
    except Exception:  # noqa: BLE001 - a step that cannot be checked does not match
        return False


def steps_text(shown: list[dict[str, Any]]) -> str:
    """The steps as plain lines, for the Inbox body and its Slack mirror."""
    lines = []
    for step in shown or []:
        if not isinstance(step, dict):
            continue
        line = f"- {step.get('kind', '')} {step.get('target', '')}"
        if step.get("destination"):
            line += f" -> {step['destination']}"
        if step.get("risky"):
            line += " (risky)"
        lines.append(line)
    return "Steps:\n" + "\n".join(lines) if lines else ""
