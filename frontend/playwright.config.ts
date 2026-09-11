import { defineConfig } from "@playwright/test";

const token = "test-internal-token-that-must-stay-server-side";

export default defineConfig({
  testDir: "./tests",
  testIgnore: "**/client-assets.test.mjs",
  fullyParallel: false,
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "../backend/.venv/bin/python -m uvicorn mock_upstream:app --app-dir tests --host 127.0.0.1 --port 9100",
      url: "http://127.0.0.1:9100/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run start -- --hostname 127.0.0.1 --port 3100",
      url: "http://127.0.0.1:3100/health",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        OPERATOR_USERNAME: "reader",
        OPERATOR_PASSWORD: "correct-horse-battery-staple",
        BACKEND_INTERNAL_URL: "http://127.0.0.1:9100",
        INTERNAL_API_TOKEN: token,
      },
    },
  ],
});
