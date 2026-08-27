# 新闻官消息网关

这个无状态网关只做三件事：验证飞书回调、提取文本消息、异步触发 GitHub Actions。它会先在飞书要求的时限内确认收件，再在后台派发任务；文字稿、摘要、状态和 OpenAI 调用都不在飞书或网关中处理。

部署 Vercel 项目时，**Root Directory 必须设为 `gateway/`**。函数固定在香港 `hkg1`，以满足飞书 URL 校验和事件确认的低延迟要求。部署后的飞书事件回调地址是 `https://<项目域名>/api`。

Vercel 环境变量：

- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_APP_ID`
- `GITHUB_DISPATCH_TOKEN`（仅授予本仓库 Actions 写权限）
- `GITHUB_REPOSITORY`（默认 `jjd200099-crypto/podcast-digest-bot`）

飞书只订阅 `im.message.receive_v1`，并且只开通以下三个应用身份权限：

- `im:message.p2p_msg:readonly`：接收私聊
- `im:message.group_at_msg:readonly`：接收群内明确 `@机器人` 的消息
- `im:message:send_as_bot`：回复和主动推送

不需要文档、通讯录、云盘、群管理或附件权限。回调加密暂未启用，生产环境依赖 HTTPS、Verification Token 和 App ID 校验。
