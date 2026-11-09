/**
 * OXP3 Status - Decky Loader Quick Access Menu panel.
 *
 * Shows the state of the OneXPlayer 3 "Anatase" fixes for the game that is
 * running right now. All work is done by the Python backend (main.py); this
 * panel only polls `get_status` and renders the report.
 *
 * The panel is defensive: a failing backend, an unexpected payload or a
 * missing field renders an "unknown"/error row instead of throwing, because the
 * Steam UI must never be taken down by a diagnostic view.
 */
import { call, toaster } from "@decky/api";
import {
  ButtonItem,
  Field,
  Focusable,
  PanelSection,
  PanelSectionRow,
  definePlugin,
  staticClasses,
} from "@decky/ui";
import { useCallback, useEffect, useRef, useState } from "react";

/** Poll interval while the panel is mounted. */
const REFRESH_MS = 5000;

type StatusState = "ok" | "warn" | "fail" | "unknown" | "na";

interface Indicator {
  id: string;
  label: string;
  state: StatusState;
  value: string;
  detail?: string | null;
  source?: string | null;
}

interface StatusGroup {
  id: string;
  title: string;
  state: StatusState;
  indicators: Indicator[];
}

interface GameInfo {
  name?: string;
  pid?: number;
  exe?: string;
  appid?: string | null;
  prefix?: string | null;
  env_readable?: boolean;
}

interface StatusReport {
  schema: number;
  version: string;
  generated_at: string;
  overall: StatusState;
  counts: Record<string, number>;
  host?: { user?: string; home?: string; kernel?: string };
  game?: GameInfo | null;
  env_sources?: { name: string; live: boolean; keys: number }[];
  groups: StatusGroup[];
  notes?: string[];
}

const STATE_COLOR: Record<StatusState, string> = {
  ok: "#59d97a",
  warn: "#f2c14e",
  fail: "#ff5f5f",
  unknown: "#9aa0a6",
  na: "#9aa0a6",
};

const STATE_LABEL: Record<StatusState, string> = {
  ok: "OK",
  warn: "WARN",
  fail: "FAIL",
  unknown: "UNKNOWN",
  na: "--",
};

function asState(value: unknown): StatusState {
  return value === "ok" || value === "warn" || value === "fail" || value === "na"
    ? value
    : "unknown";
}

function StateBadge({ state, text }: { state: StatusState; text?: string }) {
  return (
    <span
      style={{
        color: STATE_COLOR[state],
        fontWeight: 700,
        fontSize: "0.8em",
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      {text ?? STATE_LABEL[state]}
    </span>
  );
}

function IndicatorRow({
  indicator,
  expanded,
  onToggle,
}: {
  indicator: Indicator;
  expanded: boolean;
  onToggle: (id: string) => void;
}) {
  const state = asState(indicator.state);
  const hasDetail = Boolean(indicator.detail || indicator.source);
  const detail = [indicator.detail, indicator.source ? `source: ${indicator.source}` : null]
    .filter(Boolean)
    .join("\n");

  return (
    <PanelSectionRow>
      <Focusable
        onActivate={hasDetail ? () => onToggle(indicator.id) : undefined}
        style={{ display: "flex", gap: "8px", alignItems: "flex-start", padding: "2px 0" }}
      >
        <span style={{ minWidth: 62 }}>
          <StateBadge state={state} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <div style={{ lineHeight: 1.25 }}>{indicator.label}</div>
          <div style={{ opacity: 0.75, fontSize: "0.85em", lineHeight: 1.25 }}>
            {indicator.value}
          </div>
          {expanded && hasDetail && (
            <div
              style={{
                marginTop: 4,
                padding: "6px 8px",
                borderRadius: 4,
                background: "rgba(255,255,255,0.06)",
                fontFamily: "monospace",
                fontSize: "0.76em",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                opacity: 0.9,
              }}
            >
              {detail}
            </div>
          )}
        </span>
        {hasDetail && (
          <span style={{ opacity: 0.5, fontSize: "0.8em" }}>{expanded ? "-" : "+"}</span>
        )}
      </Focusable>
    </PanelSectionRow>
  );
}

function GroupSection({
  group,
  expanded,
  onToggle,
}: {
  group: StatusGroup;
  expanded: Record<string, boolean>;
  onToggle: (id: string) => void;
}) {
  const state = asState(group.state);
  return (
    <PanelSection title={`${group.title}  ·  ${STATE_LABEL[state]}`}>
      {group.indicators.map((indicator) => (
        <IndicatorRow
          key={indicator.id}
          indicator={indicator}
          expanded={Boolean(expanded[indicator.id])}
          onToggle={onToggle}
        />
      ))}
    </PanelSection>
  );
}

function useStatusReport(intervalMs: number) {
  const [report, setReport] = useState<StatusReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const inFlight = useRef(false);

  const load = useCallback(async (): Promise<StatusReport | null> => {
    if (inFlight.current) return null;
    inFlight.current = true;
    setBusy(true);
    try {
      const next = (await call("get_status")) as StatusReport;
      if (!mounted.current) return null;
      if (!next || !Array.isArray(next.groups)) {
        setError("backend returned an unexpected report");
        return null;
      }
      setReport(next);
      setError(null);
      return next;
    } catch (err) {
      if (mounted.current) {
        setError(err instanceof Error ? err.message : String(err));
      }
      return null;
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = setInterval(() => {
      void load();
    }, intervalMs);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [load, intervalMs]);

  return { report, error, busy, reload: load };
}

function StatusPanel() {
  const { report, error, busy, reload } = useStatusReport(REFRESH_MS);
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  }, []);

  const onManualRefresh = useCallback(async () => {
    const next = await reload();
    toaster.toast({
      title: "OXP3 Status",
      body: next
        ? `Refreshed: ${STATE_LABEL[asState(next.overall)]} · ${
            next.counts?.ok ?? 0
          } ok · ${next.counts?.warn ?? 0} warn · ${next.counts?.fail ?? 0} fail`
        : "Refresh failed",
    });
  }, [reload]);

  const onCopyJson = useCallback(async () => {
    if (!report) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
      toaster.toast({ title: "OXP3 Status", body: "Report copied as JSON" });
    } catch {
      toaster.toast({ title: "OXP3 Status", body: "Clipboard is not available here" });
    }
  }, [report]);

  const overall = asState(report?.overall);
  const counts = report?.counts ?? {};
  const game = report?.game ?? null;
  const notes = report?.notes ?? [];
  const envSources = report?.env_sources ?? [];

  const groups = (report?.groups ?? [])
    .map((group) => ({
      ...group,
      indicators: problemsOnly
        ? group.indicators.filter((indicator) => asState(indicator.state) !== "ok")
        : group.indicators,
    }))
    .filter((group) => group.indicators.length > 0);

  return (
    <>
      <PanelSection title="OXP3 Status">
        {!report && !error && (
          <PanelSectionRow>
            <Field label="status">
              <span style={{ opacity: 0.8 }}>loading…</span>
            </Field>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <Field
            label="Overall"
            description={
              report
                ? `${counts.ok ?? 0} ok · ${counts.warn ?? 0} warn · ${counts.fail ?? 0} fail · ${
                    counts.unknown ?? 0
                  } unknown`
                : undefined
            }
          >
            <StateBadge state={overall} text={STATE_LABEL[overall]} />
          </Field>
        </PanelSectionRow>

        <PanelSectionRow>
          <Field
            label="Game"
            description={
              game
                ? [game.appid ? `appid ${game.appid}` : null, game.pid ? `pid ${game.pid}` : null]
                    .filter(Boolean)
                    .join(" · ") || undefined
                : "no live process visible - configured launcher environment is used"
            }
          >
            <span style={{ opacity: 0.95 }}>{game?.name ?? "none detected"}</span>
          </Field>
        </PanelSectionRow>

        {error && (
          <PanelSectionRow>
            <Field label="backend" description={error}>
              <span style={{ color: STATE_COLOR.fail }}>unavailable</span>
            </Field>
          </PanelSectionRow>
        )}

        {envSources.length > 0 && (
          <PanelSectionRow>
            <Field
              label="env"
              description={envSources
                .map((source) => `${source.live ? "live" : "config"}: ${source.name} (${source.keys})`)
                .join(" · ")}
            >
              <span style={{ opacity: 0.9 }}>
                {envSources.some((source) => source.live) ? "live process env" : "configured env"}
              </span>
            </Field>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onManualRefresh} disabled={busy}>
            {busy ? "Refreshing…" : "Refresh now"}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setProblemsOnly((value) => !value)}>
            {problemsOnly ? "Show all checks" : "Show problems only"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {groups.map((group) => (
        <GroupSection key={group.id} group={group} expanded={expanded} onToggle={toggle} />
      ))}

      {report && (
        <PanelSection title="Report">
          <PanelSectionRow>
            <Field
              label="generated"
              description={`backend ${report.version} · schema ${report.schema}${
                report.host?.kernel ? ` · kernel ${report.host.kernel}` : ""
              }`}
            >
              <span style={{ opacity: 0.9 }}>{report.generated_at}</span>
            </Field>
          </PanelSectionRow>
          {notes.map((note, index) => (
            <PanelSectionRow key={`note-${index}`}>
              <Field label={`note ${index + 1}`} childrenLayout="below">
                <div style={{ opacity: 0.8, fontSize: "0.85em", whiteSpace: "pre-wrap" }}>
                  {note}
                </div>
              </Field>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={onCopyJson} disabled={!report}>
              Copy report JSON
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              label="Headless CLI"
              description="Same checks without the Steam UI; use it over SSH or when the panel cannot start."
              childrenLayout="below"
            >
              <span style={{ fontFamily: "monospace", fontSize: "0.85em" }}>
                tools/oxp3-status.sh [--json] [--strict]
              </span>
            </Field>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}

export default definePlugin(() => {
  return {
    name: "OXP3 Status",
    titleView: <div className={staticClasses.Title}>OXP3 Status</div>,
    content: <StatusPanel />,
    icon: (
      <span style={{ fontWeight: 700, fontSize: "0.72em", lineHeight: 1.1 }}>OXP3</span>
    ),
    onDismount() {
      // The panel owns its interval and clears it in useEffect cleanup; nothing
      // else to release. Kept explicit so a future watcher is not forgotten.
    },
  };
});
