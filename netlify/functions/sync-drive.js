// Sync Drive: triggers the GitHub Actions sync workflow.
// The actual sync runs in GitHub Actions (no timeout issues).

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
    const res = await fetch(
      "https://api.github.com/repos/wearepossible/photolibrary/actions/workflows/sync.yml/dispatches",
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${pat}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    if (res.status === 204) {
      return {
        statusCode: 200,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "Sync started. New photos will appear in a few minutes.",
        }),
      };
    } else {
      const body = await res.text();
      console.error("GitHub API error:", res.status, body);
      return {
        statusCode: 502,
        body: JSON.stringify({
          error: `GitHub API returned ${res.status}`,
        }),
      };
    }
  } catch (err) {
    console.error("Sync trigger error:", err);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "Failed to trigger sync: " + err.message }),
    };
  }
};
