import { useEffect, useState } from 'react';
import { Icon, type IconName } from '../navigation/icons';
import type { StageCode, StageEvent, StageState } from '../../types/retrieval';

/**
 * What the pipeline is doing, while it does it.
 *
 * Every state here arrives from the server: a stage becomes RUNNING when the code that performs it
 * begins and COMPLETED when it returns. Nothing on this page advances a stage — no timer, no
 * fraction, no estimate. A stage with no event yet is PENDING, which is the truthful reading of
 * "not announced".
 *
 * The one thing the client is allowed to count is elapsed seconds, and only after the server has
 * confirmed the request started. That is a clock, not progress: it says the machine is alive and
 * how long you have been waiting, and it claims nothing about how much is left.
 */

const STAGES: { code: StageCode; title: string; detail: string }[] = [
  {
    code: 'PREPARING',
    title: 'Preparing your question',
    detail: 'Checking scope and preparing your question.',
  },
  {
    code: 'RETRIEVAL',
    title: 'Searching indexed sources',
    detail: 'Searching your indexed documents for relevant passages.',
  },
  {
    code: 'RERANK',
    title: 'Selecting the most relevant passages',
    detail: 'Selecting the passages most relevant to your question.',
  },
  {
    code: 'EVIDENCE',
    title: 'Checking whether the evidence is sufficient',
    detail: 'Checking whether the retrieved evidence is sufficient.',
  },
  {
    code: 'GENERATION',
    title: 'Drafting an answer from the evidence',
    detail: 'Drafting an answer using only the selected evidence.',
  },
  {
    code: 'VERIFICATION',
    title: 'Checking every material claim',
    detail: 'Checking each material claim against its cited evidence.',
  },
  {
    code: 'FINALIZE',
    title: 'Preparing the verified answer and citations',
    detail: 'Preparing the verified answer.',
  },
];

/** Said only where it is true of this machine, and never as a prediction of when it will end. */
const SLOW: Partial<Record<StageCode, string>> = {
  RERANK: 'Still working. Reranking can take several seconds on this machine.',
  GENERATION: 'Still working. The drafting step is waiting on the language model.',
  VERIFICATION: 'Still working. Every claim is checked separately.',
};
const SLOW_AFTER_MS = 4000;

/** How a finished request introduces its own record, in a mark, a word and a severity. */
const OUTCOMES: Record<string, {icon: IconName; word: string; tone: string}> = {
  VERIFIED: {icon: 'verified', word: 'Verified', tone: 'verified'},
  INSUFFICIENT_EVIDENCE: {icon: 'no-answer', word: 'Not enough evidence', tone: 'caution'},
  CONFLICTING_EVIDENCE: {icon: 'conflict', word: 'Sources disagree', tone: 'caution'},
  UNVERIFIED: {icon: 'unverified', word: 'Could not verify', tone: 'caution'},
  FAILED: {icon: 'failed', word: 'Technical failure', tone: 'error'},
  OUT_OF_SCOPE: {icon: 'scope', word: 'Outside scope', tone: 'neutral'},
};

/**
 * The mark inside each stepper node.
 *
 * The node itself is a ring. A completed stage puts a tick in it, a skipped one a dash, a failed
 * one a cross; a running stage fills the ring with a dot, and a stage not yet announced leaves it
 * empty. Every one of those is also stated in words on the row beneath, so the ring is a second
 * signal rather than the only one.
 */
const MARKS: Record<StageState, IconName | 'dot' | 'empty'> = {
  COMPLETED: 'stage-done', RUNNING: 'dot', PENDING: 'empty',
  SKIPPED: 'stage-skipped', FAILED: 'stage-failed',
};

function Mark({ state }: { state: StageState }) {
  const mark = MARKS[state];
  return <span className="step-mark" data-mark={mark} aria-hidden="true">
    {mark === 'dot' ? <span className="step-dot" /> : mark === 'empty' ? null : <Icon name={mark} small />}
  </span>;
}
const WORDS: Record<StageState, string> = {
  COMPLETED: 'Completed', RUNNING: 'Running now', PENDING: 'Not started',
  SKIPPED: 'Skipped', FAILED: 'Failed',
};

/** Short labels for the submit button, so it says what is happening rather than just "working". */
export const BUSY_LABEL: Record<StageCode, string> = {
  PREPARING: 'Preparing…',
  RETRIEVAL: 'Searching sources…',
  RERANK: 'Selecting passages…',
  EVIDENCE: 'Checking evidence…',
  GENERATION: 'Drafting…',
  VERIFICATION: 'Verifying claims…',
  FINALIZE: 'Finishing…',
};

/** The stage the server last announced as running, or null when none is. */
export function runningStage(events: StageEvent[]): StageCode | null {
  const states = stageStates(events);
  return STAGES.find(stage => states[stage.code] === 'RUNNING')?.code ?? null;
}

export function stageStates(events: StageEvent[]): Record<StageCode, StageState> {
  const states = Object.fromEntries(STAGES.map(stage => [stage.code, 'PENDING'])) as
    Record<StageCode, StageState>;
  // Ordered by the server's own sequence, so a frame that arrives late cannot rewind a stage.
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    states[event.stage] = event.state;
  }
  return states;
}

/** Seconds since the first server-confirmed event. Ticks only while the request is open. */
function useElapsed(since: number | null, active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (since === null || !active) return;
    // A clock, deliberately not a stage: this interval never touches stage state.
    const handle = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(handle);
  }, [since, active]);
  // While the request is open the interval advances `now`; once it closes the interval stops and
  // the value freezes at the last tick, which is the elapsed time at the end of the request.
  return since === null ? 0 : Math.max(0, now - since);
}

export function ProgressStepper(
  { events, active, outcome }:
  { events: StageEvent[]; active: boolean; outcome?: string | null },
) {
  const states = stageStates(events);
  const running = STAGES.find(stage => states[stage.code] === 'RUNNING');
  const first = events.length ? Date.parse(events[0].started_at ?? '') || null : null;
  const elapsed = useElapsed(first, active);
  // When the current stage began, as the server reported it — not when this component noticed.
  const announced = events.filter(e => e.stage === running?.code && e.state === 'RUNNING').pop();
  const stageSince = announced?.started_at ? Date.parse(announced.started_at) || null : null;
  const stageElapsed = useElapsed(stageSince, active && Boolean(running));
  const slow = running && stageElapsed > SLOW_AFTER_MS ? SLOW[running.code] : undefined;

  if (!events.length && !active) return null;

  // Finished work collapses to one line. Seven stages are what a reader needs *while* they wait;
  // afterwards they are a record, and a record does not need to occupy the screen above the answer.
  if (!active) {
    const settled = OUTCOMES[outcome ?? ''] ?? { icon: 'verified' as IconName, word: 'Finished', tone: 'verified' };
    return <details className="progress progress-summary">
      <summary>
        <span className={`outcome-mark summary-${settled.tone}`} data-mark={settled.icon}
          aria-hidden="true"><Icon name={settled.icon} small /></span>
        <span>{settled.word}</span>
        <span className="progress-clock">{(elapsed / 1000).toFixed(1)} s</span>
        <span className="summary-hint">View processing details</span>
      </summary>
      <Stages states={states} running={undefined} stageSince={null} stageElapsed={0} slow={undefined} />
    </details>;
  }

  return <section className="progress" aria-labelledby="progress-heading">
    <div className="progress-head">
      <h2 id="progress-heading">Working through your evidence</h2>
      {first !== null &&
        <span className="progress-clock" aria-hidden="true">{(elapsed / 1000).toFixed(1)} s</span>}
    </div>

    {/* One polite region for the whole stepper: a screen reader hears the stage that changed,
        not the six that did not. */}
    <p className="visually-hidden" aria-live="polite">
      {running ? `${running.title}. Running now.`
        : outcome ? `Finished. Outcome ${outcome.replaceAll('_', ' ').toLowerCase()}.` : ''}
    </p>

    <Stages states={states} running={running} stageSince={stageSince} stageElapsed={stageElapsed}
      slow={slow} />

    <p className="muted">No answer text is shown until claim verification finishes.</p>
  </section>;
}

/** One renderer for the live stepper and for the record it collapses into. */
function Stages(
  { states, running, stageSince, stageElapsed, slow }: {
    states: Record<StageCode, StageState>;
    running: { code: StageCode; title: string; detail: string } | undefined;
    stageSince: number | null;
    stageElapsed: number;
    slow: string | undefined;
  },
) {
  return <ol className="stepper">
    {STAGES.map(stage => {
      const state = states[stage.code];
      const live = running?.code === stage.code;
      return <li key={stage.code} className={`step step-${state.toLowerCase()}`}>
        <Mark state={state} />
        <div>
          <p className="step-title">{stage.title}</p>
          {/* Never colour alone: every row states its condition in words. */}
          <p className="step-state">{WORDS[state]}</p>
          {live && <p className="step-detail">{stage.detail}</p>}
          {live && stageSince !== null &&
            <p className="step-detail mono">Current stage: {(stageElapsed / 1000).toFixed(1)} s</p>}
          {/* The clock above counts real elapsed time since the server announced this stage. It
              is not a fraction and not an estimate: nothing here predicts when the stage ends. */}
          {live && slow && <p className="step-detail">{slow}</p>}
        </div>
      </li>;
    })}
  </ol>;
}
