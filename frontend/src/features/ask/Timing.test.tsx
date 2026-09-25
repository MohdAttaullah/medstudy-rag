import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, describe } from 'vitest';
import { Timing, group } from './Timing';
import { SessionProvider } from '../library/Session';

/**
 * Where the time went.
 *
 * The rows are measurements the server reported, grouped; the total is measured at the request
 * boundary, so unattributed time stays visible instead of being absorbed into a stage.
 */

describe('timing', () => {
  const stages = [
    { stage: 'Searching sources', duration_ms: 46 },
    { stage: 'Reranking evidence', duration_ms: 7792 },
    { stage: 'Expanding context', duration_ms: 191 },
    { stage: 'Drafting from evidence', duration_ms: 3975 },
    { stage: 'Verifying claims', duration_ms: 18000 },
    { stage: 'Total', duration_ms: 31000 },
  ];

  it('groups measurements into the stages an operator reasons about', () => {
    const { rows, total, unattributed } = group(stages);
    expect(rows.map(row => row.label)).toEqual(['Retrieval', 'Reranking', 'Evidence', 'Generation', 'Verification']);
    expect(total).toBe(31000);
    // Whatever the stages do not account for stays visible rather than being absorbed.
    expect(unattributed).toBe(31000 - (46 + 7792 + 191 + 3975 + 18000));
  });

  function timing() {
    // No identity, so no diagnostics permission: this is what an ordinary reader sees.
    return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter>
      <SessionProvider><Timing stages={stages} /></SessionProvider>
    </MemoryRouter></QueryClientProvider>);
  }

  it('shows a reader the total only, without internal stage names', () => {
    timing();
    expect(screen.getByText(/Answered in 31.0 s/)).toBeInTheDocument();
    expect(screen.queryByText('Reranking')).not.toBeInTheDocument();
    expect(screen.queryByText(/Verifying claims/)).not.toBeInTheDocument();
  });
});
