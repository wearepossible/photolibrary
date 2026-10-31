// Sync Drive: the Sync button. Starts the GitHub Actions sync workflow.
// The actual sync runs in GitHub Actions (no timeout issues there).
//
// The workflow has a concurrency lock so a second triggered run will queue
// rather than race the in-progress one, but we still check here so the UI
// can show a clear "already running" message instead of silently queueing.
//
// The GitHub calls are shared with the daily trigger (scheduled-sync.js) in
// ../lib/github-sync.js.

const { startSync } = require("../lib/github-sync");

function checkAuth(event) {
  const auth = event.headers.authorization || "";
  const token = auth.replace("Bearer ", "");
  return token === process.env.ADMIN_PASSWORD;
}

exports.handler = async (event) => {
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method not allowed" };
  }

  if (!checkAuth(event)) {
    return { statusCode: 401, body: "Unauthorized" };
  }

  const pat = process.env.GITHUB_PAT;
  if (!pat) {
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "GITHUB_PAT not configured" }),
    };
  }

  try {
    const result = await startSync(pat);

    if (result.outcome === "already_running") {
      return {
        statusCode: 409,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "A sync is already running. Please wait for it to finish.",
          already_running: true,
        }),
      };
    }

    if (result.outcome === "started") {
      return {
        statusCode: 200,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "Sync started. New photos will appear in a few minutes.",
        }),
      };
    }

    console.error("GitHub API error:", result.status, result.detail);
    return {
      statusCode: 502,
      body: JSON.stringify({
        error: `GitHub API returned ${result.status}`,
      }),
    };
  } catch (err) {
    console.error("Sync trigger error:", err);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "Failed to trigger sync: " + err.message }),
    };
  }
};
