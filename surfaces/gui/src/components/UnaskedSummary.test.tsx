import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Transcript } from "./Transcript";
import { exitCodeOf } from "./UnaskedSummary";
import type { Item } from "../types";

afterEach(cleanup);

// OPE-238: after a turn in which auto-approve let actions run with no card (a gate tier or
// a reviewer "allow"), the user sees every one of them, with diffs, without opening the
// audit log. Collapsed at rest; one click expands; each item links to its audit rows.
const TURN: Item[] = [
  { kind: "user", text: "tidy the notes and run the tests" },
  { kind: "tool", id: "t1", callId: "c1", name: "read_file", args: { path: "notes.md" }, status: "ok" },
  {
    kind: "tool",
    id: "t2",
    callId: "c2",
    name: "write_file",
    args: { path: "docs/summary.md", content: "# Summary\nThree tasks done." },
    status: "ok",
    approvalOrigin: "gate_tier",
  },
  {
    kind: "tool",
    id: "t3",
    callId: "c3",
    name: "replace_in_file",
    args: { path: "src/app.py", old: "DEBUG = True", new: "DEBUG = False" },
    status: "ok",
    approvalOrigin: "reviewer",
    approvalNote: "matches the request",
  },
  {
    kind: "tool",
    id: "t4",
    callId: "c4",
    name: "run_shell",
    args: { command: "pytest -q" },
    status: "ok",
    preview: '{"command": "pytest -q", "cwd": "/w", "exit_code": 0, "output": "5 passed"}',
    approvalOrigin: "reviewer",
  },
  // Approved by the user on a card: not "without asking".
  { kind: "approval", name: "run_shell", args: { command: "make deploy" }, reason: "", resolved: "once" },
  { kind: "tool", id: "t5", callId: "c5", name: "run_shell", args: { command: "make deploy" }, status: "ok", approvalOrigin: "user" },
  { kind: "assistant", text: "Done." },
];

function openSummary() {
  fireEvent.click(screen.getByTestId("unasked-summary-head"));
  return screen.getByTestId("unasked-summary-body");
}

describe("changes made without asking (OPE-238)", () => {
  it("is collapsed at rest and counts only the unasked actions", () => {
    render(<Transcript items={TURN} onApprove={vi.fn()} />);
    expect(screen.getByTestId("unasked-summary-head").textContent).toContain("3 changes made without asking");
    expect(screen.queryByTestId("unasked-summary-body")).toBeNull();
    expect(screen.queryByText("Three tasks done.")).toBeNull();
  });

  it("sits after the turn's answer", () => {
    const { container } = render(<Transcript items={TURN} onApprove={vi.fn()} />);
    const answer = screen.getByText("Done.");
    const summary = screen.getByTestId("unasked-summary");
    // DOCUMENT_POSITION_FOLLOWING: the summary comes after the answer bubble.
    expect(answer.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container.querySelectorAll("[data-testid=unasked-summary]").length).toBe(1);
  });

  it("expands to file changes with diffs and commands with their exit status, grouped by kind", () => {
    render(<Transcript items={TURN} onApprove={vi.fn()} />);
    const body = openSummary();

    const files = within(body).getByTestId("unasked-group-files");
    const commands = within(body).getByTestId("unasked-group-commands");
    expect(within(files).getAllByTestId("unasked-item").length).toBe(2);
    expect(within(commands).getAllByTestId("unasked-item").length).toBe(1);

    // write_file: the written content, as added lines.
    const added = Array.from(files.querySelectorAll(".diff-add")).map((n) => n.textContent);
    expect(added).toContain("+# Summary");
    expect(added).toContain("+Three tasks done.");
    // replace_in_file: old text removed, new text added.
    const removed = Array.from(files.querySelectorAll(".diff-del")).map((n) => n.textContent);
    expect(removed).toContain("-DEBUG = True");
    expect(added).toContain("+DEBUG = False");
    // The full path, not just the base name: the user is checking what changed where.
    expect(files.textContent).toContain("docs/summary.md");
    expect(files.textContent).toContain("src/app.py");

    // The command, with its exit status read from the result.
    expect(commands.textContent).toContain("pytest -q");
    expect(within(commands).getByTestId("unasked-exit").textContent).toBe("exit 0");

    // Who let each one through.
    expect(files.textContent).toContain("safe change, no card");
    expect(body.textContent).toContain("auto-approved");

    // The user-approved deploy is not listed.
    expect(body.textContent).not.toContain("make deploy");
  });

  it("collapses again on a second click", () => {
    render(<Transcript items={TURN} onApprove={vi.fn()} />);
    openSummary();
    fireEvent.click(screen.getByTestId("unasked-summary-head"));
    expect(screen.queryByTestId("unasked-summary-body")).toBeNull();
  });

  it("links each item to its audit rows by tool call", () => {
    const onOpenAudit = vi.fn();
    render(<Transcript items={TURN} onApprove={vi.fn()} onOpenAudit={onOpenAudit} />);
    const body = openSummary();
    const links = within(body).getAllByTestId("unasked-audit");
    expect(links.length).toBe(3);
    fireEvent.click(links[2]);
    expect(onOpenAudit).toHaveBeenCalledWith("c4");
  });

  it("shows no audit link where the app gives none", () => {
    render(<Transcript items={TURN} onApprove={vi.fn()} />);
    expect(within(openSummary()).queryByTestId("unasked-audit")).toBeNull();
  });

  it("a failed command shows its exit status as a failure", () => {
    const items: Item[] = [
      { kind: "user", text: "run the tests" },
      {
        kind: "tool",
        id: "t1",
        callId: "c1",
        name: "run_shell",
        args: { command: "pytest -q" },
        status: "ok",
        preview: '{"command": "pytest -q", "exit_code": 1, "output": "1 failed"}',
        approvalOrigin: "reviewer",
      },
      { kind: "assistant", text: "One test fails." },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    const exit = within(openSummary()).getByTestId("unasked-exit");
    expect(exit.textContent).toBe("exit 1");
    expect(exit.className).toContain("text-danger");
  });

  it("a patch shows its own added and removed lines", () => {
    const patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old line\n+new line\n*** End Patch";
    const items: Item[] = [
      { kind: "user", text: "fix a.txt" },
      { kind: "tool", id: "t1", callId: "c1", name: "apply_patch", args: { patch }, status: "ok", approvalOrigin: "reviewer" },
      { kind: "assistant", text: "Fixed." },
    ];
    const { container } = render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(screen.getByTestId("unasked-summary-head").textContent).toContain("1 change made without asking");
    openSummary();
    expect(Array.from(container.querySelectorAll(".diff-del")).map((n) => n.textContent)).toContain("-old line");
    expect(Array.from(container.querySelectorAll(".diff-add")).map((n) => n.textContent)).toContain("+new line");
  });

  it("other actions the reviewer allowed are listed under their own heading", () => {
    const items: Item[] = [
      { kind: "user", text: "post the digest" },
      {
        kind: "tool",
        id: "t1",
        callId: "c1",
        name: "send_message",
        args: { target: "slack:T1/C9", text: "hi" },
        status: "ok",
        approvalOrigin: "reviewer",
      },
      { kind: "assistant", text: "Posted." },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    const other = within(openSummary()).getByTestId("unasked-group-other");
    expect(other.textContent).toContain("Slack");
  });

  it("is not shown while the turn is still running", () => {
    const live = TURN.slice(0, -1); // no answer yet
    render(<Transcript items={live} onApprove={vi.fn()} running />);
    expect(screen.queryByTestId("unasked-summary")).toBeNull();
  });

  it("is not shown when every action was asked or allowed by the mode", () => {
    const items: Item[] = [
      { kind: "user", text: "read the notes" },
      { kind: "tool", id: "t1", callId: "c1", name: "read_file", args: { path: "notes.md" }, status: "ok" },
      { kind: "tool", id: "t2", callId: "c2", name: "run_shell", args: { command: "ls" }, status: "ok", approvalOrigin: "bypass" },
      { kind: "assistant", text: "Read." },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(screen.queryByTestId("unasked-summary")).toBeNull();
  });

  it("a denied call is never listed as a change", () => {
    const items: Item[] = [
      { kind: "user", text: "clean up" },
      { kind: "tool", id: "t1", callId: "c1", name: "run_shell", args: { command: "rm -rf build" }, status: "denied", approvalOrigin: "reviewer_denied" },
      { kind: "assistant", text: "Not allowed." },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    expect(screen.queryByTestId("unasked-summary")).toBeNull();
  });

  it("each turn gets its own summary", () => {
    const items: Item[] = [
      ...TURN,
      { kind: "user", text: "again" },
      { kind: "tool", id: "t9", callId: "c9", name: "run_shell", args: { command: "ruff check" }, status: "ok", approvalOrigin: "reviewer" },
      { kind: "assistant", text: "Clean." },
    ];
    render(<Transcript items={items} onApprove={vi.fn()} />);
    const heads = screen.getAllByTestId("unasked-summary-head").map((h) => h.textContent);
    expect(heads.length).toBe(2);
    expect(heads[0]).toContain("3 changes");
    expect(heads[1]).toContain("1 change made");
  });
});

describe("exitCodeOf", () => {
  it("reads the exit code from a JSON result, a truncated one, or not at all", () => {
    expect(exitCodeOf('{"exit_code": 0}')).toBe(0);
    expect(exitCodeOf('{"command": "x", "cwd": "/w", "exit_code": 2, "output": "unterminated')).toBe(2);
    expect(exitCodeOf("ran: ls")).toBeNull();
    expect(exitCodeOf(undefined)).toBeNull();
    expect(exitCodeOf('{"exit_code": null}')).toBeNull();
  });
});
