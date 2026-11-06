import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
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
    baseURL: 'http://127.0.0.1:5173',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: '"' + python + '" -m backend.tests.e2e_server',
      cwd: root,
      url: 'http://127.0.0.1:8000/api/v1/health/ready',
      reuseExistingServer: false,
    },
    {
      command: 'npm run dev -- --port 5173 --strictPort',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: false,
    },
  ],
});
