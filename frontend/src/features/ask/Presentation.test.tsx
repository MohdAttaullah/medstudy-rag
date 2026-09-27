import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, describe } from 'vitest';
import { Answer } from './Answer';

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
