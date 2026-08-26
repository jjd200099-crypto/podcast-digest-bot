# 云端播客纪要机器人

GitHub Actions 每天北京时间 08:30 运行：扫描指定 YouTube 频道，只有取得完整英文字幕时才调用 OpenAI Responses API 生成中文会议纪要，并通过飞书机器人私信发送。

## GitHub Secrets

配置以下三个 Secrets 后即可启用：

- `OPENAI_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

可选 Variables：`OPENAI_MODEL`、`FEISHU_USER_OPEN_ID`。频道列表在 `feeds.json`；已处理视频记录在 `state.json`。
