# #47 与文档门槛、重试和跟进消息的交互审计

审计基线：ac765d6（含 main adf00f9），2026-10-11。仅使用临时数据库、
本地夹具和 Mock 客户端；没有调用模型、发送飞书、部署、修改 remote 或读取凭证。

## 默认门槛恢复

原先把默认门槛或默认行为写成 5 的文件，精确列表：

- `src/news_officer/config.py`：字段默认和环境变量默认均为 5。
- `src/news_officer/episode_document.py`：构造参数默认 min_stars=5。
- `src/news_officer/runtime.py`：没有 compiler/min_stars 时回退为 5。
- `README.md`：默认门槛 5、仅五星生成文档。
- `docs/editorial-policy.md`：默认 5，并要求环境显式设置 5。
- `tests/test_config.py`：默认值期望 5。
- `tests/test_episode_document.py`：默认只编译五星的行为断言。

`tests/test_reader_daily.py` 原有显式 `Mock(min_stars=5)`；这是显式配置用例，
保持不变，新增缺少 compiler 时的默认回退测试。评分体系本身的五星内容与
其他显式 min_stars=5 用例保持原义。默认现在是 4：值得看全文（4★）和
值得编译（5★）的每一期均入选精读文档，3★及以下不入选，可用
`NEWS_OFFICER_DAILY_DOCUMENT_MIN_STARS` 调整。

`Settings` 有必填参数，不能直接无参实例化；构造测试仅提供必填参数，
不传 daily_document_min_stars。环境测试用 patch.dict(clear=True) 清除覆盖项。

## 1. 每日跟进链接消息

`enqueue_followup` 仅在日报已发送、有成功文档且所有入选节目已完成或失败时
冻结链接；只接受同日，稳定 job key 为 `daily-document-followup:{today}`。
同一群的 delivery key 与 UUID 固定，outbox 唯一约束保留首次内容。
一条群消息必须能装入单个 part，超限报错。多个群分别有一条 outbox。

完成/恢复/failed 重新入队均保留 outbox；sent 条目不会再次调用 deliver。
部分失败时其他群独立发送；未确认条目退避后仍使用相同 UUID。重启处理中的
作业也保留这些记录。回执丢失后是否被远端去重依赖飞书 UUID 幂等语义；
本地测试模拟远端按 UUID 去重，不能把离线测试当成对远端的绝对交付保证。

发现的缺口：#47 的生产 FeishuMessenger 已校验 message_id，但 runtime
仍信任任何 messenger 的空返回，可能将无回执条目标为 sent。最小修复：
跟进消息在 runtime 中额外拒绝 None、空字符串和纯空白回执，将其留在 pending，
记录错误并沿既有退避恢复。测试夹具的成功 deliver 现在返回模拟 remote ID。

新增测试覆盖双群部分失败、退避推进时间、重启、发送后完成前崩溃、failed
重新入队，以及三种空回执不可完成、重试复用 UUID。确认本地不重复发送已确认
消息，远端幂等夹具中每群仅有一条消息。

## 2. 模型调用边界

ShownotesWriter.generate 每次最多 3 次 responses.parse。文档编译在生成前
调用 retry_available / _claim_attempt；episode_document_attempts 按 episode
与 document revision 持久化，不随 jobs.attempts 被恢复为 0 而重置。
默认 max_attempts=3，可用 NEWS_OFFICER_DAILY_DOCUMENT_MAX_ATTEMPTS 调整。
当日预算耗尽或跨日会将 revision 标为 failed；DocumentRetryStopped 在 worker
中以 store.fail(..., max_attempts=1) 终止当前作业，进入 operations warnings。

#47 超过六天仍扫描有未完成任务的日报；生产 enqueue_enrichment 传 compiler，
先判断 retry_available，不可用时 _stop_job。即使绕过此调度检查直接恢复任务，
publish 仍在生成前检查，不能再次调用模型。job 冷却后虽然可以再次被 enqueue，
revision 的终止状态仍然拦截生成。已成功文档/精读缓存可重放，不重新调用模型；
新原稿 revision 有独立预算。

新增测试使用真实 ShownotesWriter 与 Mock responses.parse，让每轮输出均
验证失败，推进 SQLite available_at 时间后重试：每次 3 次，总计 9 次。
耗尽后恢复/重建 compiler 的调用数为 0，job 维持 failed，operations warning
存在。另覆盖跨日且超过六天的 daily_episode 恢复，无模型调用；调度 guard
也将其终止。未发现无界模型调用缺口。

## 3. 停滞监控和 candidate 持久化

background_jobs 跟踪整项作业耗时，文档预算 3600 秒；health/watchdog
通过进程重启恢复持久化作业，不在活跃线程仍运行时开启并发重试。
recover_interrupted_jobs 仅改变 jobs 状态和退避时间，不清空 revision attempts、
job_results 或 outbox。candidate 在 provider 调用前保存，恢复保留原稿来源、
metadata、尝试数和 cooldown；这增加可恢复输入，不绕过文档预算和同日截止。
现有 test_audit_reliability 覆盖上述监控与 candidate 检查。

验证：基线 688 项，A 全量 690 项，B 全量 694 项，均通过（1 项跳过）；
selector 轮询仅在 /tmp 测试启动器中使用，不修改应用逻辑或测试断言。
ruff check src tests scripts 通过，git diff --check 通过。
