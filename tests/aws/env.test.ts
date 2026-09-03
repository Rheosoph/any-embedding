import assert from "node:assert/strict";
import test from "node:test";

import {
  numberEnvironment,
  requiredEnvironment,
  sha256Environment,
} from "../../app/aws/env.js";

test("required environment values fail fast when absent or blank", () => {
  assert.throws(
    () => requiredEnvironment("STATE_TABLE_NAME", {}),
    /STATE_TABLE_NAME is not configured/u,
  );
  assert.throws(
    () => requiredEnvironment("STATE_TABLE_NAME", { STATE_TABLE_NAME: "  " }),
    /STATE_TABLE_NAME is not configured/u,
  );
  assert.equal(
    requiredEnvironment("STATE_TABLE_NAME", { STATE_TABLE_NAME: " state " }),
    "state",
  );
});

test("numeric environment values use validated defaults and reject invalid input", () => {
  assert.equal(numberEnvironment("PORT", 8080, { integer: true, minimum: 1 }, {}), 8080);
  assert.equal(
    numberEnvironment("PORT", 8080, { integer: true, minimum: 1 }, { PORT: "9000" }),
    9000,
  );
  assert.throws(
    () => numberEnvironment("PORT", 8080, { integer: true }, { PORT: "not-a-number" }),
    /PORT must be an integer/u,
  );
  assert.throws(
    () => numberEnvironment("PORT", 8080, { integer: true, maximum: 65_535 }, {
      PORT: "70000",
    }),
    /PORT must be an integer/u,
  );
});

test("API key hashes are normalized and validated during initialization", () => {
  const uppercaseHash = "A".repeat(64);
  assert.equal(
    sha256Environment("API_KEY_SHA256", { API_KEY_SHA256: uppercaseHash }),
    uppercaseHash.toLowerCase(),
  );
  assert.throws(
    () => sha256Environment("API_KEY_SHA256", { API_KEY_SHA256: "short" }),
    /64-character SHA-256 digest/u,
  );
});
