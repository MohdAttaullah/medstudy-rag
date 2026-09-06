import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  // The dev server compiles the whole app while the first test runs, and the bundle has
  // grown with each milestone. 30s was tight enough to flake on a cold start.
  timeout: 60000,
  use: {
    baseURL: 'http://127.0.0.1:4173', browserName: 'chromium', headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL,
  },
  webServer: { command: 'npm run dev -- --port 4173 --strictPort', url: 'http://127.0.0.1:4173', reuseExistingServer: false },
});
