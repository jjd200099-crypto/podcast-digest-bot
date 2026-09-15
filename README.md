# 情报官

情报官把飞书限定为交互界面：它接收私聊或群内 @，并把结果发回飞书。播客发现、完整文字稿核验、OpenAI 摘要、定时任务、队列与状态全部运行在一个常驻云容器中，因此电脑关机后仍能工作。

服务通过飞书官方 WebSocket 长连接收消息，不需要公网回调、Vercel、Verification Token 或逐条触发 GitHub Actions。每个请求先进入 SQLite 持久队列；分析结果和每个收件人的每一段消息再固化到 outbox 后才发送。网络中断、容器重启或单个无效收件人都不会导致重新调用模型、消息混段或阻塞其他群。

代码按 `Feishu adapter → command plugins → podcast service → transcript providers → persistent store/outbox` 分层。新增搜索、公司研究等能力时，实现一个独立 plugin 并在 `CommandRouter` 注册即可；只有新能力确实要访问飞书文档、日历等资源时，才需要增量申请相应飞书权限。

## 文字稿规则

情报官优先通过节目官方 RSS 发现新集，再尝试官网的官方 transcript、RSS 声明的完整文字稿和出版方批准的 Substack 逐字稿，最后回退到公开视频字幕。只有来源明确、文本密度达标且覆盖节目主体时才会调用模型；否则固定回复“未取得完整文字稿，本次不摘要”。当前已接入 Acquired、Dwarkesh、Lenny's Podcast、The Generalist、David Senra、Sequoia、Invest Like the Best/Colossus 等官方来源。YouTube 只作为字幕与元数据补充；全链路不可用时会明确告知用户，不会把简介伪装成摘要。

摘要遵循“会议纪要核心要点精简版”：中文输出，按 3–5 个主题组织，每期恰好精选 10 条连续编号的 key takeaways。优先保留数字、强观点、反共识判断和可执行启示，并明确标注嘉宾预测、公司主张与模型估算。

## 飞书最小配置

应用名建议设为“情报官”，只需要三个应用身份权限：

- `im:message.p2p_msg:readonly`
- `im:message.group_at_msg:readonly`
- `im:message:send_as_bot`

事件只订阅 `im.message.receive_v1`，接收方式选择长连接。私聊可直接发链接或发送“订阅”“退订”“帮助”；群聊中 @ 情报官后使用相同功能。“订阅”会把当前私聊用户或当前群持久加入日报收件人。下面的官方一键注册流程会生成一个只含上述权限的确认页。确认一次后，脚本会配置 `lark-cli`，并把 App Secret 安全存入 macOS 系统钥匙串，全程不显示明文：

```bash
uv run --with lark-oapi==1.7.3 python scripts/register_feishu_app.py
```

## Railway 部署

仓库根目录的 `Dockerfile` 会被 Railway 自动识别。服务必须保持一个 replica、关闭 Serverless 休眠，并把持久卷真实挂载到 `/data`。付费计划可将 restart policy 设为 `Always`；Free/Trial 计划使用其允许的 `On Failure`（最多 10 次）。程序在 Railway 上会主动校验卷；配置错误时宁可启动失败，也不会把去重和队列状态悄悄写入临时磁盘。

配置 `.env.example` 中的变量后启动。核心变量是 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 `OPENAI_API_KEY`。`FEISHU_USER_OPEN_IDS` 与 `FEISHU_GROUP_CHAT_IDS` 是可选的初始订阅种子；此后用户和群可以直接在飞书中订阅或退订，状态保存在 SQLite。退订会跨重启保留，即使旧 ID 仍留在环境变量中也不会被重新加入。密钥应通过 Railway Variables 或 CLI stdin 写入，不进入仓库与命令行参数。

推荐使用初始化脚本。它默认只预览计划；加 `--apply` 后才会检查已登录的 Railway CLI，创建项目（或关联已有项目）、准备 `news-officer` service、挂载 `/data`，并用 `--skip-deploys` 写入变量。它不会部署服务，也不会创建或升级付费套餐：

```bash
# 首次创建：先预览，再执行
uv run scripts/bootstrap_railway.py --create-project
uv run scripts/bootstrap_railway.py --create-project --apply

# 或关联一个已有 Railway 项目
uv run scripts/bootstrap_railway.py --project-id <project-id> --apply
```

脚本从 lark-cli 当前 profile 读取 App ID，并解密其系统钥匙串引用。两个 Secret 只通过子进程 stdin 写入 Railway，既不出现在命令参数中，也不会打印或写入仓库。`OPENAI_API_KEY` 优先从同名环境变量读取；没有时使用隐藏输入。管道场景可加 `--openai-key-stdin`。初始化成功后，再单独执行脚本提示的 `railway up` 命令部署，便于把“准备配置”和“实际上线”分成两个清晰动作。

默认每天北京时间 08:30 扫描过去 72 小时内的新节目，最多摘要 3 期。所有已检查节目、消息幂等键和任务状态都保存在 `/data/news-officer.sqlite3`。

本地运行：

```bash
cp .env.example .env
docker build -t news-officer .
docker run --env-file .env -v news-officer-data:/data news-officer
```

测试：

```bash
python -m unittest discover -s tests -v
```
