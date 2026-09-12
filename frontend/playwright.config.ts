import { defineConfig } from "@playwright/test";

const token = "test-internal-token-that-must-stay-server-side";
const oauthEnvironment = {
  X_CLIENT_ID: "test-x-client-id-must-stay-server-side",
  X_OAUTH_REDIRECT_URI: "http://127.0.0.1:3100/api/x/callback",
  X_OAUTH_STATE_SECRET: "test-x-oauth-state-secret-32-bytes-minimum",
  X_OAUTH_AUTHORIZE_URL: "http://127.0.0.1:9100/mock-x/authorize",
};

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
        ...oauthEnvironment,
      },
    },
    {
      command: "npm run start -- --hostname 127.0.0.1 --port 3101",
      url: "http://127.0.0.1:3101/health",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        OPERATOR_USERNAME: "reader",
        OPERATOR_PASSWORD: "correct-horse-battery-staple",
        BACKEND_INTERNAL_URL: "http://127.0.0.1:9100",
        INTERNAL_API_TOKEN: token,
      },
    },
    {
      command: "npm run start -- --hostname 127.0.0.1 --port 3102",
      url: "http://127.0.0.1:3102/health",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        OPERATOR_USERNAME: "reader",
        OPERATOR_PASSWORD: "correct-horse-battery-staple",
        BACKEND_INTERNAL_URL: "http://127.0.0.1:9100",
        INTERNAL_API_TOKEN: token,
        ...oauthEnvironment,
        X_OAUTH_STATE_SECRET: "too-short",
      },
    },
  ],
});
