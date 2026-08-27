# 新闻官｜云端播客纪要

GitHub Actions 每天北京时间 08:30 运行：扫描指定 YouTube 频道，只有字幕覆盖节目主体且达到最低文本密度时，才调用 OpenAI Responses API 生成中文会议纪要，并通过飞书私信和指定群聊发送。

## GitHub Secrets

配置以下三个 Secrets 后即可启用：

- `OPENAI_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

可选 Variables：`OPENAI_MODEL`、`FEISHU_USER_OPEN_ID`、`FEISHU_GROUP_CHAT_IDS`（多个群用英文逗号分隔）。频道列表在 `feeds.json`；已处理视频记录在 `state.json`。

双向交互由 `gateway/` 中的无状态消息网关接收飞书事件，再异步触发 `handle-feishu-request.yml`。飞书仅作为消息入口和回复通道，不保存文字稿或摘要状态。交互应用只需私聊接收、群内 @ 接收和机器人发送三个 IM 权限；不接入文档、通讯录、云盘或群管理能力。

若交互机器人与每日推送机器人不是同一应用，GitHub 额外配置 `FEISHU_INTERACTIVE_APP_ID` 与 `FEISHU_INTERACTIVE_APP_SECRET`；否则自动复用原有凭据。网关部署说明见 `gateway/README.md`。
