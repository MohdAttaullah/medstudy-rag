import { useEffect, useState } from 'react';
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

const MARKS: Record<StageState, string> = {
  COMPLETED: '✓', RUNNING: '●', PENDING: '○', SKIPPED: '–', FAILED: '✕',
};
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

  return <section className="panel progress" aria-labelledby="progress-heading">
    <div className="question-footer">
      <h2 id="progress-heading">
        {active ? 'Working through your evidence' : 'How this answer was produced'}
      </h2>
      {active && first !== null &&
        <span className="mono" aria-hidden="true">{(elapsed / 1000).toFixed(1)} s</span>}
    </div>

    {/* One polite region for the whole stepper: a screen reader hears the stage that changed,
        not the six that did not. */}
    <p className="visually-hidden" aria-live="polite">
      {running ? `${running.title}. Running now.`
        : outcome ? `Finished. Outcome ${outcome.replaceAll('_', ' ').toLowerCase()}.` : ''}
    </p>

    <ol className="stepper">
      {STAGES.map(stage => {
        const state = states[stage.code];
        return <li key={stage.code} className={`step step-${state.toLowerCase()}`}>
          <span className="step-mark" aria-hidden="true">{MARKS[state]}</span>
          <div>
            <p className="step-title">{stage.title}</p>
            {/* Never colour alone: every row states its condition in words. */}
            <p className="step-state">{WORDS[state]}</p>
            {state === 'RUNNING' && <p className="step-detail">{stage.detail}</p>}
            {state === 'RUNNING' && stageSince !== null &&
              <p className="step-detail mono">Current stage: {(stageElapsed / 1000).toFixed(1)} s</p>}
            {state === 'RUNNING' && slow && <p className="step-detail">{slow}</p>}
          </div>
        </li>;
      })}
    </ol>

    {active && <p className="muted">
      No answer text is shown until claim verification finishes.
    </p>}
  </section>;
}
