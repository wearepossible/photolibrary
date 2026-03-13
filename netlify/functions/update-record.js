// Update metadata for a photo record in data.json (stored in R2)
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");

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

  try {
    const { id, field, value } = JSON.parse(event.body);

    if (!id || !field) {
      return { statusCode: 400, body: "Missing id or field" };
    }

    // Only allow editing specific fields
    const editableFields = ["keywords", "description", "alt_text", "campaign", "credit"];
    if (!editableFields.includes(field)) {
      return { statusCode: 400, body: `Field '${field}' is not editable` };
    }

    const r2 = getR2Client();
    const bucket = process.env.R2_BUCKET_NAME;

    // Fetch current data.json with ETag for optimistic locking
    let data, etag;
    const getCmd = new GetObjectCommand({ Bucket: bucket, Key: "data.json" });
    const getRes = await r2.send(getCmd);
    etag = getRes.ETag;
    const body = await getRes.Body.transformToString();
    data = JSON.parse(body);

    // Find and update the record
    const record = data.find((r) => r.id === id);
    if (!record) {
      return { statusCode: 404, body: `Record '${id}' not found` };
    }

    record[field] = value;

    // Write back with conditional header (optimistic locking)
    // Retry up to 3 times on conflict
    let saved = false;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const putCmd = new PutObjectCommand({
          Bucket: bucket,
          Key: "data.json",
          Body: JSON.stringify(data, null, 2),
          ContentType: "application/json",
        });
        await r2.send(putCmd);
        saved = true;
        break;
      } catch (err) {
        if (err.name === "PreconditionFailed" && attempt < 2) {
          // Re-read, re-apply, retry
          const retryRes = await r2.send(getCmd);
          const retryBody = await retryRes.Body.transformToString();
          data = JSON.parse(retryBody);
          const retryRecord = data.find((r) => r.id === id);
          if (retryRecord) retryRecord[field] = value;
          etag = retryRes.ETag;
        } else {
          throw err;
        }
      }
    }

    if (!saved) {
      return { statusCode: 409, body: "Conflict: could not save after retries" };
    }

    return {
      statusCode: 200,
      body: JSON.stringify({ ok: true, id, field }),
    };
  } catch (err) {
    console.error("Update error:", err);
    return { statusCode: 500, body: "Internal server error" };
  }
};
