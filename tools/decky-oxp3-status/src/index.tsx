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
  Spinner,
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

// ---------------------------------------------------------------------------
// Two-line overview, rendered above every other section:
//   1. whether all OXP3 fixes are applied and working (verify harness)
//   2. whether the running game loads XeSS and whether the XMX path is active
// The backend does all detection and sends ready-to-show labels; this file only
// colours them and keeps the expandable issue/evidence details.
// ---------------------------------------------------------------------------
type HealthState = "ok" | "warn" | "fail" | "error";

interface OverviewIssue {
  id: string;
  state: string;
  message?: string;
  hint?: string;
}

interface OverviewHealth {
  state?: string;
  headline?: string;
  summary?: string;
  counts?: { pass?: number; fail?: number; skip?: number; unknown?: number; total?: number };
  issues?: OverviewIssue[];
  issues_total?: number;
  issue_overflow?: number;
  error?: { code?: string; message?: string } | null;
  checked_at?: string;
}

interface OverviewXess {
  state?: string;
  label?: string;
  game?: string | null;
  pid?: number | null;
  evidence?: string;
  xess_loaded?: string[];
  xmx_evidence?: string;
}

interface Overview {
  health?: OverviewHealth;
  xess?: OverviewXess;
}

/**
 * Headless-render seed for test/overview.mjs. The Decky loader never sets this
 * global, so in Gamemode the panel always fetches get_overview from the backend.
 */
function overviewSeed(): Overview | null {
  const value = (globalThis as { __OXP3_STATUS_OVERVIEW__?: unknown })
    .__OXP3_STATUS_OVERVIEW__;
  return value && typeof value === "object" ? (value as Overview) : null;
}

/** Test-only: start with the issue/evidence details expanded. */
function overviewOpenSeed(): boolean {
  return Boolean(
    (globalThis as { __OXP3_STATUS_OVERVIEW_OPEN__?: unknown })
      .__OXP3_STATUS_OVERVIEW_OPEN__
  );
}

const HEALTH_COLOR: Record<HealthState, string> = {
  ok: STATE_COLOR.ok,
  warn: STATE_COLOR.warn,
  fail: STATE_COLOR.fail,
  error: STATE_COLOR.fail,
};

const XESS_COLOR: Record<string, string> = {
  "xess-on-xmx": STATE_COLOR.ok,
  "xess-on-standard": STATE_COLOR.warn,
  "xess-off": STATE_COLOR.unknown,
  "no-game": STATE_COLOR.unknown,
};

const OVERVIEW_REFRESH_MS = 15000;

function asHealthState(value: unknown): HealthState {
  return value === "ok" || value === "warn" || value === "fail" ? value : "error";
}

function OverviewLines({
  overview,
  error,
}: {
  overview: Overview | null;
  error: string | null;
}) {
  const [showIssues, setShowIssues] = useState(() => overviewOpenSeed());
  const [showEvidence, setShowEvidence] = useState(() => overviewOpenSeed());
  const health = overview?.health;
  const xess = overview?.xess;
  const healthState = health ? asHealthState(health.state) : "error";
  const headline =
    health?.headline ??
    (overview || error ? "Fix status unavailable" : "Checking OXP3 fixes...");
  const healthDetail =
    health?.summary ??
    (error
      ? error
      : "runs tools/oxp3-verify.sh --json (read-only, cached by the backend)");
  const issues = health?.issues ?? [];
  const overflow = health?.issue_overflow ?? 0;
  const xessLabel =
    xess?.label ??
    (overview || error ? "Game status unavailable" : "Checking the running game...");
  const xessColor = xess ? XESS_COLOR[xess.state ?? ""] ?? STATE_COLOR.unknown : STATE_COLOR.unknown;

  return (
    <PanelSection>
      <PanelSectionRow>
        <Focusable
          onActivate={issues.length > 0 ? () => setShowIssues((value) => !value) : undefined}
          style={{ display: "flex", gap: "8px", alignItems: "flex-start", padding: "2px 0" }}
        >
          <span style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                color: HEALTH_COLOR[healthState],
                fontWeight: 700,
                fontSize: "0.92em",
                lineHeight: 1.25,
              }}
            >
              {headline}
            </div>
            <div style={{ opacity: 0.7, fontSize: "0.76em", lineHeight: 1.2 }}>
              {healthDetail}
            </div>
            {showIssues && issues.length > 0 && (
              <div
                style={{
                  marginTop: 4,
                  padding: "6px 8px",
                  borderRadius: 4,
                  background: "rgba(255,255,255,0.06)",
                  fontSize: "0.76em",
                  lineHeight: 1.3,
                }}
              >
                {issues.map((issue) => (
                  <div key={issue.id} style={{ marginBottom: 4 }}>
                    <span
                      style={{
                        color:
                          issue.state === "fail"
                            ? STATE_COLOR.fail
                            : STATE_COLOR.warn,
                        fontWeight: 700,
                        fontSize: "0.9em",
                        textTransform: "uppercase",
                      }}
                    >
                      {issue.state}
                    </span>{" "}
                    <span style={{ fontFamily: "monospace" }}>{issue.id}</span>
                    {issue.message ? <span> — {issue.message}</span> : null}
                    {issue.hint ? (
                      <div
                        style={{
                          opacity: 0.78,
                          fontFamily: "monospace",
                          fontSize: "0.92em",
                          whiteSpace: "pre-wrap",
                        }}
                      >
                        {issue.hint}
                      </div>
                    ) : null}
                  </div>
                ))}
                {overflow > 0 && (
                  <div style={{ opacity: 0.7 }}>
                    +{overflow} more in the full matrix below
                  </div>
                )}
              </div>
            )}
          </span>
          {issues.length > 0 && (
            <span style={{ opacity: 0.5, fontSize: "0.8em" }}>
              {showIssues ? "-" : "+"}
            </span>
          )}
        </Focusable>
      </PanelSectionRow>
      <PanelSectionRow>
        <Focusable
          onActivate={
            xess?.evidence ? () => setShowEvidence((value) => !value) : undefined
          }
          style={{ display: "flex", gap: "8px", alignItems: "flex-start", padding: "2px 0" }}
        >
          <span style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                color: xessColor,
                fontWeight: 600,
                fontSize: "0.88em",
                lineHeight: 1.25,
              }}
            >
              {xessLabel}
            </div>
            {showEvidence && xess?.evidence && (
              <div
                style={{
                  marginTop: 2,
                  padding: "6px 8px",
                  borderRadius: 4,
                  background: "rgba(255,255,255,0.06)",
                  fontFamily: "monospace",
                  fontSize: "0.74em",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  opacity: 0.9,
                }}
              >
                {xess.evidence}
              </div>
            )}
          </span>
          {xess?.evidence && (
            <span style={{ opacity: 0.5, fontSize: "0.8em" }}>
              {showEvidence ? "-" : "+"}
            </span>
          )}
        </Focusable>
      </PanelSectionRow>
    </PanelSection>
  );
}

function useOverview() {
  const [overview, setOverview] = useState<Overview | null>(() => overviewSeed());
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const inFlight = useRef(false);

  const load = useCallback(async (refresh = false): Promise<Overview | null> => {
    if (inFlight.current) return null;
    inFlight.current = true;
    try {
      const next = (await call<[boolean], Overview>("get_overview", refresh)) as Overview;
      if (!mounted.current) return null;
      if (next && typeof next === "object") {
        setOverview(next);
        setError(null);
        return next;
      }
      setError("backend returned an unexpected overview");
      return null;
    } catch (err) {
      if (mounted.current) {
        setError(err instanceof Error ? err.message : String(err));
      }
      return null;
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    // The backend caches the verify harness result for 90 s, so polling here
    // only costs one /proc scan per interval.
    const timer = setInterval(() => {
      void load();
    }, OVERVIEW_REFRESH_MS);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [load]);

  return { overview, error, reload: load };
}

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

// ---------------------------------------------------------------------------
// Verification action: one request to the backend, which runs
// tools/oxp3-verify.sh with a hard timeout and capped output. "full" and
// "changes" always need an extra confirmation click; the backend refuses them
// without it unless the operator set OXP3_STATUS_ALLOW_ESCALATION=1.
// ---------------------------------------------------------------------------
type VerifyMode = "quick" | "full" | "changes";
type VerifyOverall = "pass" | "fail" | "skip" | "error";

interface VerifyItem {
  id: string;
  title?: string;
  summary?: string;
  hint?: string;
}

interface VerifyCounts {
  total?: number;
  pass?: number;
  fail?: number;
  skip?: number;
  unknown?: number;
}

interface VerificationResult {
  ok?: boolean;
  mode?: string;
  overall?: string;
  counts?: VerifyCounts;
  exit_code?: number | null;
  failures?: VerifyItem[];
  skips?: VerifyItem[];
  error?: { code?: string; message?: string } | null;
  started_at?: string;
  finished_at?: string;
  duration_s?: number;
  output_truncated?: boolean;
}

const VERIFY_BADGE: Record<VerifyOverall, string> = {
  pass: "PASS",
  fail: "FAIL",
  skip: "SKIP",
  error: "ERROR",
};

const VERIFY_STATE: Record<VerifyOverall, StatusState> = {
  pass: "ok",
  fail: "fail",
  skip: "warn",
  error: "fail",
};

const VERIFY_CONFIRM: Record<Exclude<VerifyMode, "quick">, string> = {
  full:
    "Runs the invasive checks: one solid LED frame and the suspend/battery attempts, which report SKIP without root. It can change live state until the RGB keeper re-applies its config.",
  changes:
    "Full, plus re-applies the speaker EQ drop-in in ~/.config/pipewire (persistent until eq-off.sh).",
};

function asVerifyOverall(value: unknown): VerifyOverall {
  return value === "pass" || value === "fail" || value === "skip" ? value : "error";
}

function verifyCountsText(result: VerificationResult): string {
  const counts = result.counts ?? {};
  return `${counts.total ?? 0} checks · ${counts.pass ?? 0} pass · ${
    counts.fail ?? 0
  } fail · ${counts.skip ?? 0} skip · ${counts.unknown ?? 0} unknown`;
}

/** The last result (or the error), rendered as compact rows. */
function VerificationSummary({ result }: { result: VerificationResult }) {
  const overall = asVerifyOverall(result.overall);
  const counts = result.counts ?? {};
  const failures = (result.failures ?? []).slice(0, 3);
  const skips = (result.skips ?? []).slice(0, 4);
  const timing =
    typeof result.duration_s === "number" ? ` · ${result.duration_s.toFixed(1)} s` : "";

  return (
    <>
      <PanelSectionRow>
        <Field
          label="Last verification"
          description={
            result.error
              ? `error: ${result.error.message ?? "unknown error"}`
              : `${verifyCountsText(result)}${timing}`
          }
        >
          <StateBadge state={VERIFY_STATE[overall]} text={VERIFY_BADGE[overall]} />
        </Field>
      </PanelSectionRow>
      {failures.map((failure) => (
        <PanelSectionRow key={`verify-fail-${failure.id}`}>
          <Field
            label={failure.id}
            description={failure.hint || failure.summary || failure.title || undefined}
          >
            <span style={{ color: STATE_COLOR.fail }}>{failure.summary || "failed"}</span>
          </Field>
        </PanelSectionRow>
      ))}
      {skips.length > 0 && (
        <PanelSectionRow>
          <Field
            label="skipped"
            description={`${counts.skip ?? 0} check(s): ${skips
              .map((skip) => skip.id)
              .join(" · ")}`}
          >
            <span style={{ opacity: 0.8 }}>SKIP</span>
          </Field>
        </PanelSectionRow>
      )}
      {result.output_truncated && (
        <PanelSectionRow>
          <Field label="note" description="the harness output was capped; re-run it from a terminal for the full report.">
            <span style={{ opacity: 0.8 }}>truncated</span>
          </Field>
        </PanelSectionRow>
      )}
    </>
  );
}

function VerificationSection({ onFinished }: { onFinished: () => void }) {
  const [result, setResult] = useState<VerificationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Exclude<VerifyMode, "quick"> | null>(null);
  const [showModes, setShowModes] = useState(false);
  const mounted = useRef(true);
  const running = useRef(false);

  useEffect(() => {
    mounted.current = true;
    void call<[], { available?: boolean; last?: VerificationResult }>("get_verification")
      .then((payload) => {
        if (mounted.current && payload?.available && payload.last) {
          setResult(payload.last);
        }
      })
      .catch(() => {
        // The section still works: the next run reports its own errors.
      });
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(
    async (mode: VerifyMode, confirm: boolean) => {
      if (running.current) return;
      running.current = true;
      setBusy(true);
      setPending(null);
      try {
        const next = await call<[VerifyMode, boolean], VerificationResult>(
          "run_verification",
          mode,
          confirm
        );
        if (mounted.current && next && typeof next === "object") {
          setResult(next);
        }
        onFinished();
        const body =
          next?.ok === true
            ? `${VERIFY_BADGE[asVerifyOverall(next.overall)]} · ${verifyCountsText(next)}`
            : `not run: ${next?.error?.message ?? "unknown error"}`;
        toaster.toast({ title: "OXP3 verification", body });
      } catch (err) {
        toaster.toast({
          title: "OXP3 verification",
          body: `call failed: ${err instanceof Error ? err.message : String(err)}`,
        });
      } finally {
        running.current = false;
        if (mounted.current) setBusy(false);
      }
    },
    [onFinished]
  );

  const overall = result ? asVerifyOverall(result.overall) : null;

  return (
    <>
      <PanelSectionRow>
        <Field
          label="Verification"
          description={
            busy
              ? "running tools/oxp3-verify.sh as the desktop user; output and time are capped"
              : result
              ? undefined
              : "runs the acceptance harness tools/oxp3-verify.sh (offline, capped, never reboots)"
          }
        >
          {busy ? (
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center", opacity: 0.9 }}>
              <Spinner /> running…
            </span>
          ) : overall ? (
            <StateBadge state={VERIFY_STATE[overall]} text={VERIFY_BADGE[overall]} />
          ) : (
            <span style={{ opacity: 0.75 }}>not run</span>
          )}
        </Field>
      </PanelSectionRow>

      {!busy && result && <VerificationSummary result={result} />}

      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => void run("quick", false)} disabled={busy}>
          {busy ? "Verifying…" : "Run quick verification"}
        </ButtonItem>
      </PanelSectionRow>

      {!busy && !showModes && !pending && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setShowModes(true)}>
            More verification modes…
          </ButtonItem>
        </PanelSectionRow>
      )}

      {!busy && showModes && !pending && (
        <>
          <PanelSectionRow>
            <Field
              label="Invasive modes"
              description="Confirmation is required. Root-only suspend steps are skipped because the harness runs as the deck user."
              childrenLayout="below"
            >
              <span style={{ opacity: 0.8, fontSize: "0.85em" }}>
                The plugin never reboots, installs or asks for sudo.
              </span>
            </Field>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setPending("full")}>
              Run full verification…
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setPending("changes")}>
              Run full + changes…
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setShowModes(false)}>
              Hide modes
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}

      {pending && (
        <>
          <PanelSectionRow>
            <Field label={`Confirm ${pending} run`} description={VERIFY_CONFIRM[pending]} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => void run(pending, true)}>
              Confirm and run {pending}
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setPending(null)}>
              Cancel
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </>
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
  const {
    overview,
    error: overviewError,
    reload: reloadOverview,
  } = useOverview();
  const [problemsOnly, setProblemsOnly] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  }, []);

  const onManualRefresh = useCallback(async () => {
    const next = await reload();
    void reloadOverview(true);
    toaster.toast({
      title: "OXP3 Status",
      body: next
        ? `Refreshed: ${STATE_LABEL[asState(next.overall)]} · ${
            next.counts?.ok ?? 0
          } ok · ${next.counts?.warn ?? 0} warn · ${next.counts?.fail ?? 0} fail`
        : "Refresh failed",
    });
  }, [reload, reloadOverview]);

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
      <OverviewLines overview={overview} error={overviewError} />
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

        <VerificationSection
          onFinished={() => {
            void reload();
            void reloadOverview(true);
          }}
        />

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
