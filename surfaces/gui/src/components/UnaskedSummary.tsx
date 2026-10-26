import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Item } from "../types";
import { humanizeTool } from "../humanize";
import { Icon } from "./Icon";

// OPE-238: "N changes made without asking". After a turn in which auto-approve let actions
// run with no approval card (a gate tier, OPE-233, or a reviewer "allow"), the user who was
// away sees every one of them here, with diffs, without opening the audit log. Collapsed at
// rest; one click expands. Built from the turn's tool items, whose approval origin is
// persisted on the tool message, so a reloaded session shows the same block.

type ToolItem = Extract<Item, { kind: "tool" }>;

// Mirrors `_UNASKED_ORIGINS` in coworker/engine.py.
const UNASKED_ORIGINS = new Set(["gate_tier", "reviewer"]);
const FILE_TOOLS = new Set(["write_file", "replace_in_file", "apply_patch", "apply_unified_diff"]);
const COMMAND_TOOLS = new Set(["run_shell"]);
// A whole-file write can be long: show its head, and say how much is left.
const MAX_DIFF_LINES = 200;

export function unaskedTools(items: Item[]): ToolItem[] {
  return items.filter(
    (it): it is ToolItem =>
      it.kind === "tool" && UNASKED_ORIGINS.has(it.approvalOrigin ?? "") && it.status !== "denied" && it.status !== "…",
  );
}

// A shell result is `{"command", "cwd", "exit_code", "output", …}`; the preview may be cut
// short, so read the field rather than parse the whole thing.
export function exitCodeOf(preview: string | undefined): number | null {
  if (!preview) return null;
  const m = /"exit_code"\s*:\s*(-?\d+)/.exec(preview);
  return m ? Number(m[1]) : null;
}

type DiffLine = { kind: "add" | "del" | "ctx" | "meta"; text: string };

function lines(text: unknown): string[] {
  const s = typeof text === "string" ? text : "";
  if (!s) return [];
  return s.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n");
}

export function diffLines(name: string, args: any): DiffLine[] {
  const a = args && typeof args === "object" ? args : {};
  switch (name) {
    case "write_file":
      return lines(a.content).map((text) => ({ kind: "add", text }));
    case "replace_in_file":
      return [
        ...lines(a.old).map((text) => ({ kind: "del" as const, text })),
        ...lines(a.new).map((text) => ({ kind: "add" as const, text })),
      ];
    case "apply_patch":
    case "apply_unified_diff":
      return lines(name === "apply_patch" ? a.patch : a.diff).map((raw) => {
        if (raw.startsWith("***") || raw.startsWith("+++") || raw.startsWith("---") || raw.startsWith("@@"))
          return { kind: "meta", text: raw };
        if (raw.startsWith("+")) return { kind: "add", text: raw.slice(1) };
        if (raw.startsWith("-")) return { kind: "del", text: raw.slice(1) };
        return { kind: "ctx", text: raw.startsWith(" ") ? raw.slice(1) : raw };
      });
    default:
      return [];
  }
}

function Diff({ tool }: { tool: ToolItem }) {
  const { t } = useTranslation();
  const all = diffLines(tool.name, tool.args);
  if (!all.length) return null;
  const shown = all.slice(0, MAX_DIFF_LINES);
  return (
    <pre
      className="ml-5 mr-2 my-1 px-2.5 py-1.5 rounded-lg border border-line bg-paper font-mono text-meta leading-relaxed whitespace-pre-wrap break-words max-h-72 overflow-auto"
      data-testid="unasked-diff"
    >
      {shown.map((l, i) =>
        l.kind === "add" ? (
          <div className="diff-add text-ok" key={i}>{"+" + l.text}</div>
        ) : l.kind === "del" ? (
          <div className="diff-del text-danger" key={i}>{"-" + l.text}</div>
        ) : l.kind === "meta" ? (
          <div className="diff-meta text-faint" key={i}>{l.text}</div>
        ) : (
          <div className="diff-ctx text-muted" key={i}>{" " + l.text}</div>
        ),
      )}
      {all.length > shown.length && (
        <div className="text-faint">{t("transcript.unasked.more_lines", { count: all.length - shown.length })}</div>
      )}
    </pre>
  );
}

function UnaskedItem({ tool, onOpenAudit }: { tool: ToolItem; onOpenAudit?: (callId: string) => void }) {
  const { t } = useTranslation();
  const line = humanizeTool(tool.name, tool.args);
  const path = typeof tool.args?.path === "string" ? tool.args.path : "";
  const isCommand = COMMAND_TOOLS.has(tool.name);
  const exit = isCommand ? exitCodeOf(tool.preview) : null;
  const failed = tool.status !== "ok";
  return (
    <div data-testid="unasked-item" className="py-0.5">
      <div className="flex items-baseline gap-2 px-2">
        <span className={"w-3.5 text-center text-label shrink-0 " + (failed || (exit !== null && exit !== 0) ? "text-danger" : "text-ok")}>●</span>
        <span className="min-w-0 text-ui leading-relaxed">
          {isCommand ? (
            <code className="font-mono text-meta text-ink break-all">{String(tool.args?.command ?? "")}</code>
          ) : path ? (
            <>
              <span className="text-muted">{line.pre}</span>
              <span className="font-mono text-meta text-ink break-all">{path}</span>
            </>
          ) : (
            <>
              <span className="text-muted">{line.pre}</span>
              {line.obj && <span className="text-ink">{line.obj}</span>}
              {line.post && <span className="text-muted">{line.post}</span>}
            </>
          )}
        </span>
        {exit !== null ? (
          <span className={"text-label shrink-0 " + (exit === 0 ? "text-ok" : "text-danger")} data-testid="unasked-exit">
            {t("transcript.unasked.exit", { code: exit })}
          </span>
        ) : (
          failed && <span className="text-label text-danger shrink-0">{tool.status}</span>
        )}
        <span
          className="text-label text-faint shrink-0"
          title={
            tool.approvalOrigin === "gate_tier"
              ? t("transcript.approval.gate_tier_title")
              : tool.approvalNote || t("transcript.approval.reviewer_allowed_title")
          }
        >
          {tool.approvalOrigin === "gate_tier" ? t("transcript.approval.gate_tier") : t("transcript.approval.auto_approved")}
        </span>
        {onOpenAudit && tool.callId && (
          <button
            className="ml-auto shrink-0 text-label text-faint hover:text-muted cursor-pointer"
            data-testid="unasked-audit"
            onClick={() => onOpenAudit(tool.callId!)}
          >
            {t("transcript.unasked.audit")}
          </button>
        )}
      </div>
      <Diff tool={tool} />
    </div>
  );
}

export function UnaskedSummary({ tools, onOpenAudit }: { tools: ToolItem[]; onOpenAudit?: (callId: string) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (!tools.length) return null;
  const groups: Array<{ key: "files" | "commands" | "other"; items: ToolItem[] }> = [
    { key: "files" as const, items: tools.filter((x) => FILE_TOOLS.has(x.name)) },
    { key: "commands" as const, items: tools.filter((x) => COMMAND_TOOLS.has(x.name)) },
    { key: "other" as const, items: tools.filter((x) => !FILE_TOOLS.has(x.name) && !COMMAND_TOOLS.has(x.name)) },
  ].filter((g) => g.items.length);
  return (
    <details className="stepgroup unasked-summary" open={open} data-testid="unasked-summary">
      <summary
        className="stepgroup-head transcript-disclosure cursor-pointer select-none text-ui text-faint hover:text-muted"
        data-testid="unasked-summary-head"
        onClick={(e) => {
          e.preventDefault(); // drive open/closed from state, not the native toggle
          setOpen(!open);
        }}
      >
        <Icon name="chevronDown" size={12} />
        <span>{t("transcript.unasked.summary", { count: tools.length })}</span>
      </summary>
      {open && (
        <div className="ml-1.5 mt-1 pl-2 border-l-2 border-line flex flex-col gap-1.5" data-testid="unasked-summary-body">
          {groups.map((g) => (
            <div key={g.key} data-testid={`unasked-group-${g.key}`}>
              <div className="px-2 text-label text-faint uppercase tracking-wide">{t(`transcript.unasked.group_${g.key}`)}</div>
              {g.items.map((tool, i) => (
                <UnaskedItem tool={tool} onOpenAudit={onOpenAudit} key={tool.id || i} />
              ))}
            </div>
          ))}
        </div>
      )}
    </details>
  );
}
