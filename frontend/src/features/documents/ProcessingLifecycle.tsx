import { useEffect, useRef, useState } from 'react';
import { Icon, type IconName } from '../navigation/icons';
import {
  ACTIVITY_LABEL, duration, estimateText, factLine, sinceOnServer, STAGE_LABEL, STATE,
} from './lifecycle';
import { useNow } from './useLifecycle';
import type { Lifecycle, LifecycleStageState } from '../../types/lifecycle';

/**
 * The document's journey to "ready for Ask", shown as it actually happens.
 *
 * Each stage's state is the server's. The only thing that moves on its own is the elapsed clock,
 * which counts from the server's own timestamps, and the spinner on the stage that is running.
 * There is no percentage: reading a scanned book can take an hour and building its keyword index
 * a second, so "four of seven stages done" would say nothing true about what is left.
 */

const MARK: Record<LifecycleStageState, IconName> = {
  COMPLETED: 'stage-done',
  RUNNING: 'processing',
  PENDING: 'stage-skipped',
  BLOCKED: 'conflict',
  FAILED: 'stage-failed',
  CANCELLED: 'stage-skipped',
};

/** The state of each stage in words, so nothing depends on colour or on the mark. */
export const STAGE_STATE_WORD: Record<LifecycleStageState, string> = {
  COMPLETED: 'Completed',
  RUNNING: 'In progress',
  PENDING: 'Not started',
  BLOCKED: 'Needs review',
  FAILED: 'Failed',
  CANCELLED: 'Stopped',
};

/** A worker that has not been heard from for this long is reported as quiet, not as stuck. */
const QUIET_AFTER_MS = 120_000;

export function ProcessingLifecycle(
  { lifecycle, receivedAt }: { lifecycle: Lifecycle; receivedAt: number },
) {
  const running = !lifecycle.terminal;
  const now = useNow(running);
  const state = STATE[lifecycle.state];
  // A finished document reports the time its stages actually took, summed. The span of the last
  // run would understate it after a rechunk (seconds, against minutes of reading), and the span
  // from first upload would count the idle days between a stop and a retry as processing.
  const recorded = lifecycle.stages.filter(stage => stage.duration_ms !== null);
  const elapsed = lifecycle.terminal
    ? recorded.length ? recorded.reduce((total, stage) => total + stage.duration_ms!, 0) : null
    : sinceOnServer(lifecycle.run_started_at, lifecycle.server_time, receivedAt, now);
  // A run restarted part-way (rechunk, re-embed) counts from its restart, and says so.
  const restarted = lifecycle.stages.some(stage => stage.started_at && lifecycle.run_started_at
    && Date.parse(stage.started_at) < Date.parse(lifecycle.run_started_at));
  const heartbeat = sinceOnServer(lifecycle.heartbeat_at, lifecycle.server_time, receivedAt, now);
  const estimate = running ? estimateText(lifecycle.estimate) : null;
  const ready = lifecycle.state === 'READY';

  // One announcement per change of stage or state. Elapsed seconds are never announced.
  const [announcement, setAnnouncement] = useState('');
  const last = useRef('');
  useEffect(() => {
    const stage = lifecycle.current_stage ? STAGE_LABEL[lifecycle.current_stage] : '';
    const said = `${state.title}${stage && !ready ? `. ${stage}` : ''}.`;
    if (last.current && last.current !== said) setAnnouncement(said);
    last.current = said;
  }, [lifecycle.current_stage, lifecycle.state, state.title, ready]);

  return <section className={`lifecycle tone-${state.tone}`} aria-labelledby="lifecycle-title">
    <div className="lifecycle-head">
      <div className="lifecycle-title">
        <span className="lifecycle-mark" aria-hidden="true"><Icon name={state.icon} /></span>
        <div>
          <h2 id="lifecycle-title">{state.title}</h2>
          {running && lifecycle.activity &&
            <p className="lifecycle-activity">{ACTIVITY_LABEL[lifecycle.activity] ?? lifecycle.activity}</p>}
        </div>
      </div>
      <dl className="lifecycle-clock">
        {elapsed !== null && <div>
          <dt>{running ? restarted ? 'Elapsed in this run' : 'Elapsed' : 'Processing time'}</dt>
          <dd><span className="num">{duration(elapsed)}</span></dd>
        </div>}
        <div>
          <dt>Ready for Ask</dt>
          <dd>{ready ? 'Yes' : 'Not yet'}</dd>
        </div>
      </dl>
    </div>

    {(estimate || (running && heartbeat !== null)) && <div className="lifecycle-notes">
      {estimate && <p className="lifecycle-estimate">{estimate}</p>}
      {running && heartbeat !== null && <p className="lifecycle-heartbeat">
        {heartbeat < QUIET_AFTER_MS
          ? <>Worker active — last signal <span className="num">{duration(heartbeat)}</span> ago.</>
          : <>No signal from the worker for <span className="num">{duration(heartbeat)}</span>. A large
            scanned document can take a while between signals; if this continues, an
            administrator can check the Operations page.</>}
      </p>}
    </div>}

    <p className="visually-hidden" aria-live="polite">{announcement}</p>

    <ol className="lifecycle-steps">
      {lifecycle.stages.map(stage => {
        const facts = factLine(stage);
        const live = stage.state === 'RUNNING';
        const since = live
          ? sinceOnServer(stage.started_at, lifecycle.server_time, receivedAt, now)
          : null;
        return <li key={stage.code} className={`lc-step lc-${stage.state.toLowerCase()}`}
          aria-current={live || stage.state === 'BLOCKED' ? 'step' : undefined}>
          <span className="lc-step-icon" aria-hidden="true"><Icon name={MARK[stage.state]} small /></span>
          <div className="lc-step-body">
            <p className="lc-step-name">{STAGE_LABEL[stage.code]}</p>
            <p className="lc-step-meta">
              <span className="lc-step-word">{STAGE_STATE_WORD[stage.state]}</span>
              {stage.duration_ms !== null && stage.state === 'COMPLETED' && stage.code !== 'READY' &&
                <> · <span className="num">{duration(stage.duration_ms)}</span></>}
              {since !== null && <> · running for <span className="num">{duration(since)}</span></>}
            </p>
            {facts && <p className="lc-step-facts">{facts}</p>}
          </div>
        </li>;
      })}
    </ol>
  </section>;
}
