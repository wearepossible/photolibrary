// Daily sync trigger.
//
// Netlify runs this on the schedule set in netlify.toml (06:00 UTC). It starts
// the same GitHub Actions workflow as the Sync button.
//
// Why here and not a `schedule:` trigger in the workflow itself: GitHub
// switches off scheduled workflows in public repos after 60 days without a
// commit. That silently stopped the daily sync between July and October
// 2026. A workflow that is only ever started on request isn't affected.
//
// Scheduled functions can't be called by URL in production, so this needs no
// password check. Netlify only runs the schedule on the published site, not
// on deploy previews; to test a preview, use "Run now" on the function's page
// in the Netlify UI.

const { startSync } = require("../lib/github-sync");

exports.handler = async () => {
  const pat = process.env.GITHUB_PAT;
  if (!pat) {
    console.error("Scheduled sync: GITHUB_PAT not configured");
    return { statusCode: 500 };
  }

  try {
    const result = await startSync(pat);
    if (result.outcome === "error") {
      console.error("Scheduled sync: GitHub API error:", result.status, result.detail);
      return { statusCode: 502 };
    }
    // "started" or "already_running": either way, a sync is happening.
    console.log(`Scheduled sync: ${result.outcome}`);
    return { statusCode: 200 };
  } catch (err) {
    console.error("Scheduled sync error:", err);
    return { statusCode: 500 };
  }
};
