// Verify the admin password (server-side check)
exports.handler = async (event) => {
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method not allowed" };
  }

  try {
    const { password } = JSON.parse(event.body);
    const correct = process.env.ADMIN_PASSWORD;

    if (!correct) {
      return { statusCode: 500, body: "ADMIN_PASSWORD not configured" };
    }

    if (password === correct) {
      return {
        statusCode: 200,
        body: JSON.stringify({ ok: true }),
      };
    }

    return { statusCode: 401, body: "Incorrect password" };
  } catch {
    return { statusCode: 400, body: "Invalid request" };
  }
};
