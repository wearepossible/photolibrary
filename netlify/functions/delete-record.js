// Delete a photo record from data.json (stored in R2)
const { S3Client, GetObjectCommand, PutObjectCommand, DeleteObjectCommand } = require("@aws-sdk/client-s3");

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
    const { id } = JSON.parse(event.body);

    if (!id) {
      return { statusCode: 400, body: "Missing id" };
    }

    const r2 = getR2Client();
    const bucket = process.env.R2_BUCKET_NAME;

    // Fetch current data.json
    const getCmd = new GetObjectCommand({ Bucket: bucket, Key: "data.json" });
    const getRes = await r2.send(getCmd);
    const body = await getRes.Body.transformToString();
    const data = JSON.parse(body);

    // Find the record
    const idx = data.findIndex((r) => r.id === id);
    if (idx === -1) {
      return { statusCode: 404, body: `Record '${id}' not found` };
    }

    const record = data[idx];

    // Remove the record
    data.splice(idx, 1);

    // Delete thumbnail from R2
    try {
      await r2.send(new DeleteObjectCommand({
        Bucket: bucket,
        Key: `thumbnails/${id}.jpg`,
      }));
    } catch { /* thumbnail may not exist */ }

    // Delete alternative thumbnails
    for (const alt of record.alternatives || []) {
      const altName = alt.filename.replace(/\.[^.]+$/, ".jpg");
      try {
        await r2.send(new DeleteObjectCommand({
          Bucket: bucket,
          Key: `thumbnails/${altName}`,
        }));
      } catch { /* ignore */ }
    }

    // Write updated data.json
    await r2.send(new PutObjectCommand({
      Bucket: bucket,
      Key: "data.json",
      Body: JSON.stringify(data, null, 2),
      ContentType: "application/json",
    }));

    return {
      statusCode: 200,
      body: JSON.stringify({ ok: true, id, remaining: data.length }),
    };
  } catch (err) {
    console.error("Delete error:", err);
    return { statusCode: 500, body: "Internal server error" };
  }
};
