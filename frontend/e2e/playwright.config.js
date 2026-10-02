// The e2e suite (D-F11-1): `npm run e2e --prefix frontend`. Real backend, real Streamlit,
// headless Chromium. See docs/steps/F11.md.
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: /.*\.spec\.js$/,
  globalSetup: require.resolve('./servers.js'),
  outputDir: 'test-results',
  timeout: 240_000,
  expect: { timeout: 60_000 },
  fullyParallel: true,
  workers: Number(process.env.E2E_WORKERS || 3),
  retries: 0,
  reporter: [['list']],
  use: {
    headless: true,
    viewport: { width: 1440, height: 900 },
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
