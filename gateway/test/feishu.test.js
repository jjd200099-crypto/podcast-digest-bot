import assert from "node:assert/strict";
import test from "node:test";

import { RequestError, parseFeishuPayload, workflowPayload } from "../lib/feishu.js";


const token = "verification-token";
const appId = "cli_test";


function messageEnvelope(overrides = {}) {
  return {
    schema: "2.0",
    header: {
      token,
      app_id: appId,
      event_type: "im.message.receive_v1",
    },
    event: {
      sender: { sender_type: "user" },
      message: {
        message_id: "om_123",
        message_type: "text",
        chat_type: "p2p",
        content: JSON.stringify({ text: "请分析 https://youtu.be/example" }),
        ...overrides,
      },
    },
  };
}


test("parses a user message and keys work by message_id", () => {
  const parsed = parseFeishuPayload(messageEnvelope(), token, appId);
  assert.deepEqual(parsed, {
    kind: "message",
    messageId: "om_123",
    text: "请分析 https://youtu.be/example",
  });
  const payload = workflowPayload(parsed.messageId, parsed.text);
  assert.equal(payload.inputs.message_id, "om_123");
  assert.equal("event_id" in payload.inputs, false);
});


test("ignores bot messages and group messages without an at-mention", () => {
  const bot = messageEnvelope();
  bot.event.sender.sender_type = "bot";
  assert.equal(parseFeishuPayload(bot, token, appId).reason, "bot_sender");

  const group = messageEnvelope({ chat_type: "group", mentions: [] });
  assert.equal(parseFeishuPayload(group, token, appId).reason, "group_message_without_mention");
});


test("requires both verification token and app id", () => {
  assert.throws(
    () => parseFeishuPayload(messageEnvelope(), "wrong-token", appId),
    (error) => error instanceof RequestError && error.status === 403,
  );
  assert.throws(
    () => parseFeishuPayload(messageEnvelope(), token, "cli_other"),
    (error) => error instanceof RequestError && error.status === 403,
  );
});
