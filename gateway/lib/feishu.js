import { timingSafeEqual } from "node:crypto";


export const MAX_BODY_BYTES = 256_000;
const MESSAGE_ID_RE = /^om_[A-Za-z0-9_-]{1,128}$/;


export class RequestError extends Error {
  constructor(status, code) {
    super(code);
    this.name = "RequestError";
    this.status = status;
    this.code = code;
  }
}


export function secureEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ""));
  const rightBuffer = Buffer.from(String(right || ""));
  return (
    leftBuffer.length > 0 &&
    leftBuffer.length === rightBuffer.length &&
    timingSafeEqual(leftBuffer, rightBuffer)
  );
}


function objectOrError(value, code) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new RequestError(400, code);
  }
  return value;
}


export function parseFeishuPayload(body, verificationToken, expectedAppId) {
  const envelope = objectOrError(body, "invalid_event_envelope");
  if (envelope.type === "url_verification") {
    if (!secureEqual(envelope.token, verificationToken)) {
      throw new RequestError(403, "invalid_verification_token");
    }
    return { kind: "challenge", challenge: String(envelope.challenge || "") };
  }
  if (envelope.encrypt) {
    throw new RequestError(400, "encrypted_callbacks_not_configured");
  }

  const header = objectOrError(envelope.header, "invalid_event_header");
  if (!secureEqual(header.token, verificationToken)) {
    throw new RequestError(403, "invalid_verification_token");
  }
  if (!expectedAppId || header.app_id !== expectedAppId) {
    throw new RequestError(403, "unexpected_app_id");
  }
  if (header.event_type !== "im.message.receive_v1") {
    return { kind: "ignored", reason: "event_type" };
  }

  const event = objectOrError(envelope.event, "invalid_event_body");
  const sender = objectOrError(event.sender, "invalid_event_sender");
  if (["bot", "app"].includes(sender.sender_type)) {
    return { kind: "ignored", reason: "bot_sender" };
  }
  const message = objectOrError(event.message, "invalid_event_message");
  if (message.message_type !== "text") {
    return { kind: "ignored", reason: "non_text_message" };
  }
  if (message.chat_type === "group" && !(message.mentions || []).length) {
    return { kind: "ignored", reason: "group_message_without_mention" };
  }

  const messageId = String(message.message_id || "");
  if (!MESSAGE_ID_RE.test(messageId)) {
    throw new RequestError(400, "invalid_message_id");
  }
  let content;
  try {
    content = JSON.parse(message.content || "{}");
  } catch {
    throw new RequestError(400, "invalid_message_content");
  }
  const text = String(content?.text || "").trim();
  if (!text) {
    throw new RequestError(400, "empty_message_text");
  }
  return { kind: "message", messageId, text: text.slice(0, 10_000) };
}


export function workflowPayload(messageId, text, ref = "main") {
  return {
    ref,
    inputs: {
      message_id: messageId,
      request_text: text.slice(0, 10_000),
    },
  };
}
