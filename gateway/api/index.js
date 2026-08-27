import { waitUntil } from "@vercel/functions";

import {
  MAX_BODY_BYTES,
  RequestError,
  parseFeishuPayload,
  workflowPayload,
} from "../lib/feishu.js";


function sendJson(response, status, body) {
  const payload = JSON.stringify(body);
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Content-Length", Buffer.byteLength(payload));
  response.end(payload);
}


async function readJsonBody(request) {
  if (request.body !== undefined) {
    const raw = Buffer.isBuffer(request.body)
      ? request.body.toString("utf8")
      : typeof request.body === "string"
        ? request.body
        : JSON.stringify(request.body);
    if (Buffer.byteLength(raw) > MAX_BODY_BYTES) {
      throw new RequestError(413, "invalid_body_size");
    }
    try {
      return typeof request.body === "object" && !Buffer.isBuffer(request.body)
        ? request.body
        : JSON.parse(raw);
    } catch {
      throw new RequestError(400, "invalid_json");
    }
  }

  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      throw new RequestError(413, "invalid_body_size");
    }
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new RequestError(400, "invalid_json");
  }
}


async function dispatchToGitHub(messageId, text) {
  const repository = process.env.GITHUB_REPOSITORY || "jjd200099-crypto/podcast-digest-bot";
  const workflow = process.env.GITHUB_WORKFLOW_FILE || "handle-feishu-request.yml";
  const ref = process.env.GITHUB_WORKFLOW_REF || "main";
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const url = `https://api.github.com/repos/${repository}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json; charset=utf-8",
          "User-Agent": "news-officer-feishu-gateway",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify(workflowPayload(messageId, text, ref)),
        signal: AbortSignal.timeout(2_500),
      });
      if (response.status === 204) {
        return;
      }
      lastError = new Error(`GitHub dispatch returned HTTP ${response.status}`);
      if (response.status !== 429 && response.status < 500) {
        break;
      }
    } catch (error) {
      lastError = error;
    }
    if (attempt < 2) {
      await new Promise((resolve) => setTimeout(resolve, 300 * (2 ** attempt)));
    }
  }
  throw lastError || new Error("GitHub dispatch failed");
}


export default async function handler(request, response) {
  if (request.method === "GET") {
    sendJson(response, 200, { ok: true, service: "news-officer-feishu-gateway" });
    return;
  }
  if (request.method !== "POST") {
    sendJson(response, 405, { ok: false, error: "method_not_allowed" });
    return;
  }

  const verificationToken = process.env.FEISHU_VERIFICATION_TOKEN || "";
  const expectedAppId = process.env.FEISHU_APP_ID || "";
  if (!verificationToken || !expectedAppId) {
    sendJson(response, 503, { ok: false, error: "gateway_not_configured" });
    return;
  }

  let parsed;
  try {
    parsed = parseFeishuPayload(await readJsonBody(request), verificationToken, expectedAppId);
  } catch (error) {
    if (error instanceof RequestError) {
      sendJson(response, error.status, { ok: false, error: error.code });
      return;
    }
    sendJson(response, 500, { ok: false, error: "internal_error" });
    return;
  }

  if (parsed.kind === "challenge") {
    sendJson(response, 200, { challenge: parsed.challenge });
    return;
  }
  if (parsed.kind === "ignored") {
    sendJson(response, 200, { ok: true, ignored: parsed.reason });
    return;
  }
  if (!process.env.GITHUB_DISPATCH_TOKEN) {
    sendJson(response, 503, { ok: false, error: "dispatch_not_configured" });
    return;
  }

  waitUntil(
    dispatchToGitHub(parsed.messageId, parsed.text).catch((error) => {
      console.error(`GitHub workflow dispatch failed: ${error.name}`);
    }),
  );
  sendJson(response, 200, { ok: true });
}
