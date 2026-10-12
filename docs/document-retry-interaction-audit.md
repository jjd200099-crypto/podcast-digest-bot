# #47 与文档门槛、重试的交互审计

审计基线：ac765d6（含 main adf00f9），2026-10-11。仅使用临时数据库、
本地夹具和 Mock 客户端；没有调用模型、发送飞书、部署、修改 remote 或读取凭证。

## 默认门槛恢复

原先默认门槛修复所涉及的文件，当前状态如下：

- `src/news_officer/config.py`：字段默认和环境变量默认均为 4。
- `src/news_officer/episode_document.py`：构造参数默认 min_stars=4。
- `src/news_officer/runtime.py`：没有 compiler/min_stars 时回退为 4。
- `README.md`：默认门槛 4、四星及五星生成文档。
- `docs/editorial-policy.md`：默认 4。
- `tests/test_config.py`：默认值期望 4。
- `tests/test_episode_document.py`：默认编译四星及五星的行为断言。

`tests/test_reader_daily.py` 原有显式 `Mock(min_stars=5)`；这是显式配置用例，
保持不变，新增缺少 compiler 时的默认回退测试。评分体系本身的五星内容与
其他显式 min_stars=5 用例保持原义。默认现在是 4：值得看全文（4★）和
值得编译（5★）的每一期均入选精读文档，3★及以下不入选，可用
`NEWS_OFFICER_DAILY_DOCUMENT_MIN_STARS` 调整。

`Settings` 有必填参数，不能直接无参实例化；构造测试仅提供必填参数，
不传 daily_document_min_stars。环境测试用 patch.dict(clear=True) 清除覆盖项。

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


## 2026-10-11：45 分钟后台健康预算

存在超时中断风险。runtime._worker 给普通日报登记 2700 秒预算；
health_snapshot 超过预算返回不健康，watch_health 持续不健康 120 秒后抛异常。
runtime.run 的 FIRST_COMPLETED 监督随之退出并取消各 worker；进程监督可重启，
外部 /healthz 也返回 503。正在运行的阻塞线程不能安全取消，持久化作业随后恢复，
但这仍可能打断日报或同时执行的文档作业。operations 的“日报逾期 45 分钟”
只是业务告警，与这个后台预算独立。

summarizer 的共享模型客户端 timeout=180、max_retries=2；shownotes 验证最多
3 次调用，单期在网络超时与 SDK 重试叠加时可消耗约 3×3×180=1620 秒，
两期串行就可能超过 2700 秒（尚未计入网络退避、检索、写入等）。完整中文转写
按批执行，每批最多 3 次调用，文档的 3600 秒预算也不能保证覆盖多段最坏耗时。
阅读版默认将文档独立入队，降低日报本身超预算的概率，但文档 watchdog 仍影响整个进程。
这个估算说明风险，不代表严格总耗时上界。

新增 NEWS_OFFICER_DAILY_HEALTH_BUDGET_SECONDS，沿用 _positive_int，默认 2700，
无效或非正值回退默认；配置可放宽普通日报预算。准备任务 900 秒、文档 3600 秒、
watchdog 120 秒宽限和业务告警均未改变。按负载调整只能降低风险，不能保证模型及时返回。
