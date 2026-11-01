import { defineConfig } from '@playwright/test'
import { existsSync } from 'node:fs'

const edge = process.env.PLAYWRIGHT_EDGE_PATH ?? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
export default defineConfig({
  testDir: './e2e',
  timeout: 300_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  fullyParallel: false,
  outputDir: './test_results/artifacts',
  reporter: [['list'], ['json', { outputFile: './test_results/results.json' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8011',
    browserName: 'chromium',
    viewport: { width: 1600, height: 1000 },
    launchOptions: { executablePath: existsSync(edge) ? edge : undefined },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    acceptDownloads: true,
  },
})
