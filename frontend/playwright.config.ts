import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const apiPort = process.env.E2E_API_PORT || '18000';
const webPort = process.env.E2E_WEB_PORT || '15173';
const apiOrigin = `http://127.0.0.1:${apiPort}`;
const python = fileURLToPath(
  new URL(
    process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python',
    import.meta.url,
  ),
);

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: '"' + python + '" -m backend.tests.e2e_server',
      cwd: root,
      env: { E2E_API_PORT: apiPort, E2E_WEB_PORT: webPort },
      url: `${apiOrigin}/api/v1/health/ready`,
      reuseExistingServer: false,
    },
    {
      command: `npm run dev -- --port ${webPort} --strictPort`,
      env: { API_PROXY_TARGET: apiOrigin },
      url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false,
    },
  ],
});
