// Shared helpers for starting the Drive sync, which runs as a GitHub Actions
// workflow. Used by two Netlify Functions:
//   - sync-drive.js     the Sync button (password-protected)
//   - scheduled-sync.js the daily trigger (runs on Netlify's schedule)
//
// Both need a fine-grained GitHub token with Actions read/write on this repo,
// set as GITHUB_PAT in Netlify's environment variables.
//
// This file lives outside netlify/functions/ so Netlify doesn't publish it as
// a function of its own; the functions that require() it bundle it in.

const REPO = "wearepossible/photolibrary";
const WORKFLOW = "sync.yml";
const API = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}`;

function githubHeaders(pat) {
  return {
    "Authorization": `Bearer ${pat}`,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

// True if a sync run is queued or in progress. If GitHub can't be asked,
// returns false and lets the dispatch call report the real problem.
async function isSyncRunning(pat) {
  const res = await fetch(`${API}/runs?per_page=5`, { headers: githubHeaders(pat) });
  if (!res.ok) return false;
  const data = await res.json();
  return (data.workflow_runs || []).some(
    (r) => r.status === "queued" || r.status === "in_progress"
  );
}

// Ask GitHub to start the sync workflow on main. Returns one of:
//   { outcome: "started" }
//   { outcome: "already_running" }
//   { outcome: "error", status, detail }   (GitHub refused or errored)
async function startSync(pat) {
  if (await isSyncRunning(pat)) {
    return { outcome: "already_running" };
  }

  const res = await fetch(`${API}/dispatches`, {
    method: "POST",
    headers: githubHeaders(pat),
    body: JSON.stringify({ ref: "main" }),
  });

  // GitHub answers 204 No Content when the run has been queued.
  if (res.status === 204) {
    return { outcome: "started" };
  }
  return { outcome: "error", status: res.status, detail: await res.text() };
}

module.exports = { startSync };
