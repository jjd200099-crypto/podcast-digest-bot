# 情报官

2026-09-17：基于 **OpenAI Agents SDK** 的[独立播客研究 Agent](docs/PODCAST_AGENT.md)已在 Railway 启用公开播客档案模式，支持来源查询、对话新增追踪、更新查询、全文问答及文字稿附件。组织飞书文档归档/检索尚未启用，与上述功能独立；新部署仍需显式配置模式与访问白名单。

情报官把飞书限定为交互界面：它接收私聊或群内 @，并把结果发回飞书。播客发现、完整文字稿核验、OpenAI 摘要、定时任务、队列与状态全部运行在一个常驻云容器中，因此电脑关机后仍能工作。

私聊与已授权群聊使用同一个 Agent、同一份正式播客全文库和日报归档。在群里任何成员都可以 @ 机器人提问；私聊访问仍由用户白名单控制。对话历史按会话和发言人隔离，不将私聊或其他同事的历史用作回答依据。`发一下今天的日报` 通过 `get_daily_digest` 读取当日已生成的正式日报，不等同于查询过去 24 小时 RSS 目录；未生成时明确说明。新节目分析保留 RSS 音频与字幕元数据，走与日报相同的全文获取链路。

服务通过飞书官方 WebSocket 长连接收消息，不需要公网回调、Vercel、Verification Token 或逐条触发 GitHub Actions。每个请求先进入 SQLite 持久队列；分析结果和每个收件人的每一段消息再固化到 outbox 后才发送。网络中断、容器重启或单个无效收件人都不会导致重新调用模型、消息混段或阻塞其他群。

代码按 `Feishu adapter → command plugins → podcast service → transcript providers → persistent store/outbox` 分层。新增搜索、公司研究等能力时，实现一个独立 plugin 并在 `CommandRouter` 注册即可；只有新能力确实要访问飞书文档、日历等资源时，才需要增量申请相应飞书权限。

## 文字稿规则

情报官优先通过节目官方 RSS 发现新集，再尝试官网的官方 transcript、RSS 声明的完整文字稿和出版方批准的 Substack 逐字稿，最后回退到公开视频字幕。只有来源明确、文本密度达标且覆盖节目主体时才会调用模型；否则固定回复“未取得完整文字稿，本次不摘要”。当前已接入 Acquired、Dwarkesh、Lenny's Podcast、The Generalist、David Senra、Sequoia、Invest Like the Best/Colossus 等官方来源。YouTube 只作为字幕与元数据补充；全链路不可用时会明确告知用户，不会把简介伪装成摘要。

摘要遵循“会议纪要核心要点精简版”：先写基于全文的推荐理由，再精选 3–8 条连续编号的主要内容（最多 10 条，信息少时更少），最后给出 1–5 星的编辑推荐。每条只写一个核心结论及一个关键依据，以 40–80 个中文字符为目标，硬上限 120 个可见正文字符。保留强观点、反共识判断和可执行启示，明确标注嘉宾预测、公司主张与模型估算。星级评价信息增量、论证质量和研究相关性，不是投资收益预测。

日报优先处理 RSS 正集，避免 YouTube 切片挤占候选名额；RSS 每源至少扫描最近 30 条，再按追踪窗口筛选。已有更新但缺少全文、日期未核验、摘要格式失败时，会单独列出数量和部分节目，不再误报“今日无可摘要”。候选上限仍然有效，状态报告不声称覆盖所有更新。

### 可选 Podwise 全文来源

配置云端 Secret `PODWISE_API_TOKEN` 后，在官网适配器与 YouTube 字幕后尝试 Podwise。需要 Podwise Pro/Enterprise，在 Settings → Developer 生成 token；不要把 token 提交到 Git、粘贴到聊天或日志中。未配置时不访问 Podwise，不能宣称已接通。

顺序为已有核验归档 → 官网/RSS 全文 → 公共字幕 → Podwise。仅使用官方 Open API 的读取接口，不调用处理/导入接口，不发起付费转写。按原始链接匹配，或严格匹配标题、节目名、日期和时长；RSS 音频与 YouTube 均有转写时优先精确匹配 RSS enclosure，同一音频多个已处理版本仍拒绝歧义。获取后再次核对身份，检查首尾与中间覆盖、文本密度，搜索片段和 AI 摘要不充当全文。时间轴小幅超出元数据时，只有差异不超过 5%、独立 status 接口确认 done/100、且较长时间轴本身通过全文覆盖校验才接受；缺中段仍拒绝。仅有起点时间戳时采用更保守门槛。当前文本密度规则面向英文播客。

授权前只能验证模拟接口，真实覆盖率需配置 token 后确认。可运行 `scripts/smoke_podwise.py` 检查四个来源各最新一期、过去 72 小时内的节目；加 `--summarize` 会使用现有模型测试其中最短一期的全文摘要和附件，仅写临时数据库，不发送飞书消息。这不是全来源覆盖率报告。

真实接口兼容性：Podwise 的原始链接可能是音频地址，需与 RSS enclosure 匹配；未处理的重复索引不阻挡唯一已转写版本，但多个已转写匹配仍拒绝猜选。数字时间戳的秒/毫秒单位以独立的可读时间戳逐段交叉核验，不按数值大小猜测。容许源音频末尾小幅超出 RSS 标注时长，但首尾与中段完整性门槛不变。

401 表示 token 问题，402 表示套餐/用量受限，429 表示限流；失败不降级为猜测摘要。参考 [授权文档](https://docs.podwise.ai/open-api-v1/basics/authorization)、[搜索 API](https://docs.podwise.ai/open-api-v1/discovery/search-episodes)、[文字稿 API](https://docs.podwise.ai/open-api-v1/get-episode-transcripts)。

## 精编文字稿与交互问答

成功生成摘要后，情报官仍把核验过的全文保存在云端，用于后续问答；**日报和链接分析默认不发送文字稿附件**。只推送推荐理由、十条以内短要点、推荐星级及收听链接。用户明确索取文字稿时才生成 Markdown 阅读版；清理只作用于阅读版，不覆盖原始全文或其校验值。

`NEWS_OFFICER_MAX_SUMMARIES=0` 与 `NEWS_OFFICER_MAX_CANDIDATES=0` 表示处理追踪时间窗内发现的全部未推送候选，不再精选三期后停止；RSS 扫描不再只取前 30 条。已推送去重、缺全文重试冷却、日期和全文核验继续生效。所有取得完整正文的节目均生成摘要及推荐；缺全文仍不能凭标题编写推荐。可用正整数恢复数量限制。`NEWS_OFFICER_DAILY_TRANSCRIPT_ATTACHMENTS=false` 为默认值；显式设为 `true` 可恢复日报附件。多期处理顺序执行，数量增加会延长运行时间并增加模型用量。

自然语言是默认交互方式。情报官会先判断用户是在问候、找节目、索取文字稿、追问节目内容，还是管理日报订阅；模型只负责意图和节目定位，不能直接编造播客答案。节目选定后，事实回答仍由完整原始文字稿和逐字引用校验约束。正文明确提到的新节目优先于被回复消息和旧会话上下文，避免在错误节目上作答。

可以直接这样说：

- `Sam Altman 在 Dwarkesh 那期怎么看 Agent？`
- `那他为什么这么判断？`
- `把这期完整文字稿发我。`
- `最近有什么值得听的？`
- `帮我订阅每天的播客日报。`

情报官会保留最近 4 轮有界对话历史，用于理解“他”“这期”“刚才第二点”等追问；这段历史只用于消解指代，不能作为节目事实证据。匹配到多期时可以自然地说“第二个”“最新那期”或回复标题关键词。

以下旧命令继续兼容；在群聊中先 @ 情报官：

- `最近播客`：列出资料库中最近可提问的节目及编号。
- `文字稿 <编号/节目/嘉宾>`：获取匹配节目的精编可读版 Markdown 文字稿；原始核验全文仍用于问答与核验。
- `问 <编号> <问题>`：针对指定节目提问，例如 `问 a1b2c3d4 嘉宾如何判断 AI 应用的护城河？`。
- 也可以自然地问 `整理某某的观点`、`某某为什么看好这个市场？`。如果当前对话已经选中过一期节目，情报官会在没有新节目锚点时沿用该上下文。
- 当节目或嘉宾名称匹配到多期内容时，情报官会返回候选列表；回复 `选 1`、`选 2` 等即可继续。

所有回答都只依据资料库中的原始、完整、可核验文字稿，并附上内部原文定位编号。文字稿没有明确说到的内容会直接说明“文字稿中未找到依据”，不会用节目简介、常识或模型推测补齐。若原始文字稿没有可靠的说话人标签，回答也会提示归因限制。

## 飞书最小配置

应用名建议设为“情报官”，只需要四个应用身份权限：

- `im:message.p2p_msg:readonly`
- `im:message.group_at_msg:readonly`
- `im:message:send_as_bot`
- `im:resource`

事件只订阅 `im.message.receive_v1`，接收方式选择长连接。`im:resource` 仅用于上传和发送精编文字稿文件。私聊可直接发链接或发送“订阅”“退订”“帮助”；群聊中 @ 情报官后使用相同功能。“订阅”会把当前私聊用户或当前群持久加入日报收件人。下面的官方一键注册流程会生成一个只含上述权限的确认页。确认一次后，脚本会配置 `lark-cli`，并把 App Secret 安全存入 macOS 系统钥匙串，全程不显示明文：

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

## 一起贡献

项目通过 GitHub Pull Request 协作。请从最新的 `main` 创建短生命周期分支，完成修改和测试后提交 PR；不要直接把功能分支长期堆在 `main` 上。CI 会自动执行 Ruff、单元测试与 Python 编译检查。

新增播客源时，PR 必须说明节目官方来源、完整文字稿的取得方式，以及文字稿不完整时的失败行为。任何 App Secret、API Key、用户或群聊 ID 都不得提交到仓库；本地配置放在未跟踪的 `.env`，云端密钥放在 Railway Variables。

完整开发流程、质量要求和 PR 清单见 [CONTRIBUTING.md](CONTRIBUTING.md)。漏洞或密钥泄露请按 [SECURITY.md](SECURITY.md) 私下报告，不要公开创建 Issue。
