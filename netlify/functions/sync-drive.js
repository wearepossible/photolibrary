// Sync Drive: scan Google Drive for new/removed files and update data.json
// Due to the 10s Netlify timeout, this function does a quick scan:
// 1. Lists all image files on Drive
// 2. Compares against current data.json
// 3. Reports new/removed files
// 4. For a small number of new files, processes them inline
// For large batches, returns a summary for manual processing via Python scripts.

const { google } = require("googleapis");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");

const IMAGE_MIMES = new Set([
  "image/jpeg", "image/png", "image/gif",
  "image/bmp", "image/tiff", "image/webp", "image/heic",
]);

function checkAuth(event) {
  const auth = event.headers.authorization || "";
  const token = auth.replace("Bearer ", "");
  return token === process.env.ADMIN_PASSWORD;
}

function getR2Client() {
  return new S3Client({
    region: "auto",
    endpoint: `https://${process.env.R2_ACCOUNT_ID}.${process.env.R2_JURISDICTION ? process.env.R2_JURISDICTION + "." : ""}r2.cloudflarestorage.com`,
    credentials: {
      accessKeyId: process.env.R2_ACCESS_KEY_ID,
      secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
    },
  });
}

function getDriveService() {
  const keyJson = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY_JSON);
  const auth = new google.auth.GoogleAuth({
    credentials: keyJson,
    scopes: ["https://www.googleapis.com/auth/drive.readonly"],
  });
  return google.drive({ version: "v3", auth });
}

async function listDriveFiles(drive, driveId) {
  const files = [];
  let pageToken = null;

  do {
    const res = await drive.files.list({
      q: "trashed = false",
      driveId,
      corpora: "drive",
      includeItemsFromAllDrives: true,
      supportsAllDrives: true,
      fields: "nextPageToken, files(id, name, mimeType, md5Checksum, size, parents, imageMediaMetadata)",
      pageSize: 1000,
      pageToken,
    });

    for (const f of res.data.files || []) {
      if (IMAGE_MIMES.has(f.mimeType)) {
        files.push({
          id: f.id,
          name: f.name,
          mimeType: f.mimeType,
          md5: f.md5Checksum || "",
          size: parseInt(f.size || "0", 10),
          parentId: (f.parents || [])[0] || "",
          width: f.imageMediaMetadata?.width || null,
          height: f.imageMediaMetadata?.height || null,
        });
      }
    }

    pageToken = res.data.nextPageToken;
  } while (pageToken);

  return files;
}

exports.handler = async (event) => {
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method not allowed" };
  }

  if (!checkAuth(event)) {
    return { statusCode: 401, body: "Unauthorized" };
  }

  try {
    const drive = getDriveService();
    const driveId = process.env.SHARED_DRIVE_ID;
    const r2 = getR2Client();
    const bucket = process.env.R2_BUCKET_NAME;

    // Fetch current data.json from R2
    const getCmd = new GetObjectCommand({ Bucket: bucket, Key: "data.json" });
    const getRes = await r2.send(getCmd);
    const dataBody = await getRes.Body.transformToString();
    const records = JSON.parse(dataBody);

    // Build a set of all known Drive file IDs (from locations + alternatives)
    const knownFileIds = new Set();
    for (const r of records) {
      for (const loc of r.locations || []) {
        knownFileIds.add(loc.drive_file_id);
      }
      for (const alt of r.alternatives || []) {
        knownFileIds.add(alt.drive_file_id);
      }
    }

    // Scan Drive
    const driveFiles = await listDriveFiles(drive, driveId);
    const driveFileIds = new Set(driveFiles.map((f) => f.id));

    // Find new files (on Drive but not in data.json)
    const newFiles = driveFiles.filter((f) => !knownFileIds.has(f.id));

    // Find removed files (in data.json but no longer on Drive)
    const removedIds = [];
    for (const id of knownFileIds) {
      if (!driveFileIds.has(id)) {
        removedIds.push(id);
      }
    }

    // Remove records where ALL locations and alternatives are gone
    let removedCount = 0;
    const updatedRecords = records.filter((r) => {
      const locsRemaining = (r.locations || []).filter(
        (l) => driveFileIds.has(l.drive_file_id)
      );
      const altsRemaining = (r.alternatives || []).filter(
        (a) => driveFileIds.has(a.drive_file_id)
      );

      if (locsRemaining.length === 0 && altsRemaining.length === 0) {
        removedCount++;
        return false;
      }

      // Update locations/alternatives to remove missing ones
      r.locations = locsRemaining;
      r.alternatives = altsRemaining;

      // Update top-level drive links if best location changed
      if (locsRemaining.length > 0) {
        r.drive_file_url = `https://drive.google.com/file/d/${locsRemaining[0].drive_file_id}/view`;
        r.drive_folder_url = `https://drive.google.com/drive/folders/${locsRemaining[0].folder_id}`;
      }

      return true;
    });

    // Save updated data.json if there were removals
    if (removedCount > 0) {
      await r2.send(new PutObjectCommand({
        Bucket: bucket,
        Key: "data.json",
        Body: JSON.stringify(updatedRecords, null, 2),
        ContentType: "application/json",
      }));
    }

    return {
      statusCode: 200,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scanned: driveFiles.length,
        existing: records.length,
        added: 0,  // New files need manual processing via Python scripts
        removed: removedCount,
        newFilesFound: newFiles.length,
        message: newFiles.length > 0
          ? `Found ${newFiles.length} new files on Drive. Run the Python sync script to process them (generate thumbnails + AI analysis).`
          : removedCount > 0
            ? `Removed ${removedCount} records for files no longer on Drive.`
            : "Everything is up to date.",
      }),
    };
  } catch (err) {
    console.error("Sync error:", err);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "Sync failed: " + err.message }),
    };
  }
};
