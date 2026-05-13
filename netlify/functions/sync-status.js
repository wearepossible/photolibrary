// Sync Status: reports whether a sync is currently running.
//
// The UI uses this to decide which icon to show next to the Sync button:
//   - spinner: a run is queued or in_progress on GitHub Actions
//   - tick:    no run is active and the last completed run succeeded
//   - cross:   no run is active and the last completed run failed
//
// The "summary of the last sync" comes from sync-status.json in R2, which
// the frontend fetches directly (it's already configured for R2 access).
// This function only reports the live GitHub Actions state.

const REPO = "wearepossible/photolibrary";
const WORKFLOW = "sync.yml";

exports.handler = async (event) => {
  if (event.httpMethod !== "GET") {
    return { statusCode: 405, body: "Method not allowed" };
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
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=5`,
      {
        headers: {
          "Authorization": `Bearer ${pat}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
      }
    );

    if (!res.ok) {
      return {
        statusCode: 502,
        body: JSON.stringify({ error: `GitHub API returned ${res.status}` }),
      };
    }

    const data = await res.json();
    const runs = data.workflow_runs || [];
    const active = runs.find(
      (r) => r.status === "queued" || r.status === "in_progress"
    );
    const lastCompleted = runs.find(
      (r) => r.status === "completed"
    );

    return {
      statusCode: 200,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      },
      body: JSON.stringify({
        running: Boolean(active),
        active_run: active
          ? {
              id: active.id,
              started_at: active.run_started_at || active.created_at,
              status: active.status,
              html_url: active.html_url,
            }
          : null,
        last_run: lastCompleted
          ? {
              id: lastCompleted.id,
              conclusion: lastCompleted.conclusion, // success | failure | cancelled | timed_out | ...
              completed_at: lastCompleted.updated_at,
              html_url: lastCompleted.html_url,
            }
          : null,
      }),
    };
  } catch (err) {
    console.error("Sync status error:", err);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "Failed to fetch sync status: " + err.message }),
    };
  }
};
