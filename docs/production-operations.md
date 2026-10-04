# 发布与验收

`main` 是协作和发布基线。功能分支向 `main` 提 PR，测试通过再合并。不要部署未提交工作目录，不把远端 PR 状态等同于线上版本。历史文档中带日期的部署记录仅代表当时状态。

统一发布入口（默认只检查）：

```sh
python scripts/release.py --project YOUR_RAILWAY_PROJECT
python scripts/release.py --project YOUR_RAILWAY_PROJECT --deploy
```

脚本要求远端当前 `main` 的 `test` 与 `hermes-contract` 均成功，只打包该提交，写入 `release.json`，不包含未提交文件或本地密钥。上传后必须确认 Railway deployment `SUCCESS`、`/healthz`、容器内 `release.json` 和非敏感业务状态。具备 GitHub Write 不自动意味着具备 Railway 发布权限。当前是可核验的人工触发发布入口，不声称已经配置 GitHub 自动部署凭证。

## 早报与监控

`NEWS_OFFICER_DAILY_PREPARATION=true` 时，定时早报前两小时开始一轮最多 12 期的预处理，与日报共用单个处理 worker，避免共享 provider 状态并发冲突。获取/评级结果可缓存；模型、提示词或证据版本变化不复用预处理缓存，已经冻结的原稿摘要版本仍按原有不可变归档规则保存。每期开始前检查 8 分钟处理预算，但不能打断已在进行的外部请求；过时的准备任务不在早报前 30 分钟重新开始。准备任务不发消息、不创建文档、不消耗功能公告。

这能减少早报时重复处理，不是严格 08:30 截止的独立采集/组装架构。早报仍需处理最新候选；完整架构拆分需另做持久化候选调度和截止时间测试。

```sh
python -m news_officer.operations --db /data/news-officer.sqlite3
```

状态区分未到时间、处理中、部分发送、全部发送和超时。计划时间后 45 分钟仍未完成则记录业务错误；统计待全文数量与超过 72 小时积压，不输出原文、用户标识或凭证。服务每分钟检查、仅状态变化时写日志。此检查不触发进程重启，避免反复打断工作。Agent 的 `get_delivery_health` 也可读这些统计。日志告警尚未绑定外部值班通知渠道。

## 对话验收

CI 覆盖引用归属、公司/嘉宾检索、会话隔离、同事群聊准入、不可编造操作和丢回执重试。另运行：

```sh
PYTHONPATH=src python scripts/evaluate_conversation_cases.py
```

此脚本调用真实模型，但用合成全文与隔离数据库，回放同事问候、引用“这篇”、连续追问、偏好登记、需求登记和上线状态追问。绝不连接飞书发消息，不代表实际群聊端到端验收，也不能替代真实播客事实核验。需要人工阅读输出，不能只看自动分数。已有 `smoke_episode_discovery.py` 等脚本用于真实来源的 opt-in 验收，勿在 CI 自动消耗生产额度。

## 可验证备份

```sh
python -m news_officer.backup --db /data/news-officer.sqlite3 --memory /data/podcast-memory --output /data/backups/UNIQUE_SNAPSHOT
python -m news_officer.backup --output /data/backups/UNIQUE_SNAPSHOT --verify-only
```

通过 SQLite 在线备份取得一致性快照，保留文字稿历史文件，再依快照重建阅读索引。进行数据库完整性和文件 SHA-256 验证。目标必须是新的专用目录；不覆盖在线库、不自动删除旧备份。数据库含私聊与群聊状态，目录权限为私有，禁止上传公开仓库。

恢复演练应将备份复制到隔离目录，复验 hash 与 integrity_check 并抽查全文，然后才规划生产切换；不能直接把备份文件覆盖运行中的 SQLite/WAL。`/data/backups` 仍在同一持久卷上，**不是异地灾备**。异地存储、加密密钥、保留周期以及费用需另行配置并定期演练；当前不假装已经具备。

## 发现观察池

关键词可以通过 `NEWS_OFFICER_DISCOVERY_TOPICS` 配置，仍受总扫描预算约束。列表外频道在 30 天内有两期不同的“值得看全文”或更高内容后，其已核验 Podwise 频道编号进入最多 20 个观察目录；后续继续检查日期目录。重复处理同一期不累计次数，过期自动退出。此观察池不改正式订阅，不执行 Podwise follow 或付费转写。
