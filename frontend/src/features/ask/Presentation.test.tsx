import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, describe } from 'vitest';
import { Answer } from './Answer';
import { questionRefusal } from './refusals';

/**
 * Outcome presentation.
 *
 * Severity is carried by colour *and* by a mark *and* by words, because a reader who cannot
 * distinguish amber from red must still be able to tell "the sources did not cover this" from
 * "the service broke" — those call for different actions.
 */

const base = {
  outcome: 'UNVERIFIED' as const, verified: false, answer: null,
  message: 'Evidence was found, but a supported answer could not be verified against it.',
  reason_codes: [] as string[], citations: [], sources: [], figures: [],
};

function show(result: Parameters<typeof Answer>[0]['result']) {
  return render(<MemoryRouter><Answer result={result} /></MemoryRouter>);
}

describe('outcome severity', () => {
  it.each([
    ['VERIFIED', 'outcome-verified', 'verified'],
    ['INSUFFICIENT_EVIDENCE', 'outcome-caution', 'no-answer'],
    ['CONFLICTING_EVIDENCE', 'outcome-caution', 'conflict'],
    ['UNVERIFIED', 'outcome-warning', 'unverified'],
    ['FAILED', 'outcome-error', 'failed'],
    ['OUT_OF_SCOPE', 'outcome-neutral', 'scope'],
  ])('%s carries its own severity class and mark', (outcome, severity, mark) => {
    const { container } = show({
      ...base,
      outcome: outcome as typeof base.outcome,
      verified: outcome === 'VERIFIED',
      answer: outcome === 'VERIFIED' ? 'A checked statement.' : null,
    });
    const panel = container.querySelector(`.${severity}`);
    expect(panel).toBeTruthy();
    // The mark is an icon rather than a typographic character now, so what is asserted is that
    // the severity really does carry a non-colour marker of its own — `data-mark` names it —
    // and that the marker is drawn.
    const drawn = panel!.querySelector(`.outcome-mark[data-mark="${mark}"]`);
    expect(drawn).toBeTruthy();
    expect(drawn!.querySelector('svg')).toBeTruthy();
  });

  it('gives every severity a different mark, so the states are not told apart by colour', () => {
    const marks = new Set<string>();
    for (const outcome of ['VERIFIED', 'INSUFFICIENT_EVIDENCE', 'CONFLICTING_EVIDENCE', 'UNVERIFIED', 'FAILED', 'OUT_OF_SCOPE']) {
      const { container, unmount } = show({
        ...base, outcome: outcome as typeof base.outcome,
        verified: outcome === 'VERIFIED',
        answer: outcome === 'VERIFIED' ? 'A checked statement.' : null,
      });
      marks.add(container.querySelector('.outcome-mark')!.getAttribute('data-mark')!);
      unmount();
    }
    // Five distinct marks across six outcomes: only the two "caution" states share a severity,
    // and even those carry different marks and different words.
    expect(marks.size).toBe(6);
  });

  it('never relies on colour alone: every state also differs in words', () => {
    const labels = new Set<string>();
    for (const outcome of ['INSUFFICIENT_EVIDENCE', 'CONFLICTING_EVIDENCE', 'UNVERIFIED', 'FAILED', 'OUT_OF_SCOPE']) {
      const { container, unmount } = show({ ...base, outcome: outcome as typeof base.outcome });
      labels.add(container.querySelector('.eyebrow')!.textContent!.trim());
      unmount();
    }
    // Five outcomes, five distinct labels: the words alone identify the state.
    expect(labels.size).toBe(5);
  });

  it('marks are decorative, so a screen reader hears the words and not the glyph', () => {
    const { container } = show({ ...base, outcome: 'FAILED' });
    expect(container.querySelector('.outcome-mark')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText(/Technical failure/)).toBeInTheDocument();
  });

  it('explains a reason code in plain words and keeps the code beside it', () => {
    show({ ...base, reason_codes: ['CLAIM_NOT_CITED'] });
    expect(screen.getByText(/carried no citation/)).toBeInTheDocument();
    expect(screen.getByText('CLAIM_NOT_CITED')).toBeInTheDocument();
  });

  it('shows an unrecognised code rather than hiding it', () => {
    show({ ...base, reason_codes: ['SOME_NEW_CODE'] });
    expect(screen.getByText('SOME_NEW_CODE')).toBeInTheDocument();
  });

  it('shows no draft text for an unverified outcome', () => {
    show({ ...base, outcome: 'UNVERIFIED' });
    expect(screen.queryByRole('heading', { name: 'Answer' })).not.toBeInTheDocument();
  });
});

/**
 * What a reader is told when the request did not produce an answer.
 *
 * Found in manual testing: a 49-word clinical vignette exceeded the query encoder's limit and was
 * refused — deliberately, never truncated — and the page said "Technical failure / The answering
 * service failed". Nothing had failed. These pin the three cases apart: a question the reader can
 * fix, a real outage, and an abstention about the evidence.
 */
describe('refusals, failures and abstentions are told apart', () => {
  it('matches only the closed set of refusal codes, never an inherited name', () => {
    expect(questionRefusal('FAILED', ['QUERY_TOO_LONG'])?.label).toBe('Question too long');
    expect(questionRefusal('FAILED', ['constructor', 'toString'])).toBeNull();
    expect(questionRefusal('INSUFFICIENT_EVIDENCE', ['QUERY_TOO_LONG'])).toBeNull();
  });
  it('names a question refused for its length, and does not call it a technical failure', () => {
    show({
      ...base, outcome: 'FAILED',
      message: 'This question is longer than the search can accept, so it was not answered.',
      reason_codes: ['QUERY_TOO_LONG'],
    });
    expect(screen.getByText('Question too long')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Please ask a shorter question' })).toBeInTheDocument();
    expect(screen.queryByText(/Technical failure/)).toBeNull();
    expect(screen.queryByText(/answering service failed/i)).toBeNull();
    // Why it was refused is readable in plain words, with the code beside it.
    expect(screen.getByText(/refused rather than shortened/)).toBeInTheDocument();
    expect(screen.getByText('QUERY_TOO_LONG')).toBeInTheDocument();
  });

  it('draws the refusal as something to act on, not as a broken service', () => {
    const { container } = show({ ...base, outcome: 'FAILED', reason_codes: ['QUERY_TOO_LONG'] });
    expect(container.querySelector('.outcome-caution')).toBeTruthy();
    expect(container.querySelector('.outcome-error')).toBeNull();
    expect(container.querySelector('.outcome-mark[data-mark="rephrase"] svg')).toBeTruthy();
  });

  it('still calls a genuine provider failure a technical failure', () => {
    const { container } = show({
      ...base, outcome: 'FAILED', reason_codes: ['GENERATION_PROVIDER_UNAVAILABLE'],
    });
    expect(screen.getByText('Technical failure')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'The answering service failed' })).toBeInTheDocument();
    expect(container.querySelector('.outcome-error')).toBeTruthy();
  });

  it('still calls a failure with no declared reason a technical failure', () => {
    show({ ...base, outcome: 'FAILED', reason_codes: [] });
    expect(screen.getByText('Technical failure')).toBeInTheDocument();
  });

  it.each([
    [['ASSESSMENT_ONLY_EVIDENCE', 'ADVISORY_CONTEXT_OMISSION']],
    [['RETRIEVAL_CORPUS_EMPTY']],
  ])('never presents an abstention (%s) as a technical failure', reasons => {
    const { container } = show({ ...base, outcome: 'INSUFFICIENT_EVIDENCE', reason_codes: reasons });
    expect(screen.getByText('Not enough evidence')).toBeInTheDocument();
    expect(screen.queryByText(/Technical failure/)).toBeNull();
    expect(container.querySelector('.outcome-error')).toBeNull();
  });

  it('shows no answer text on any of them', () => {
    for (const reason_codes of [['QUERY_TOO_LONG'], ['GENERATION_PROVIDER_UNAVAILABLE']]) {
      const { unmount } = show({ ...base, outcome: 'FAILED', reason_codes });
      expect(screen.queryByRole('heading', { name: 'Answer' })).toBeNull();
      unmount();
    }
  });
});
