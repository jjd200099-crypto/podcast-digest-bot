from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping
from datetime import datetime

from lark_channel import (
    ChatQueueConfig,
    Events,
    FeishuChannel,
    LogLevel,
    PolicyConfig,
    SafetyConfig,
    TextBatchConfig,
)

from .config import Settings
from .daily_report import coverage_report
from .feishu import FeishuMessenger, delivery_parts, file_delivery_part
from .health import health_snapshot, serve_health, watch_health
from .models import DailyItem, IncomingMessage, Job, TranscriptAttachment
from .podcast import PodcastService
from .qa import render_transcript_attachment
from .research_checkpoint import ResearchContinuationPending
from .router import CommandRouter
from .store import Store
from .transcript_view import RENDERER_VERSION

logger = logging.getLogger(__name__)


class NewsOfficerRuntime:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        messenger: FeishuMessenger,
        router: CommandRouter,
        podcast_service: PodcastService,
        research_agent=None,
    ):
        self.settings = settings
        self.store = store
        self.messenger = messenger
        self.router = router
        self.podcast_service = podcast_service
        self.research_agent = research_agent
        self.active_jobs = {}
        self.wake_workers = {
            "message": asyncio.Event(),
            "daily": asyncio.Event(),
        }
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self.channel = FeishuChannel(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            # The SDK's INFO connection line contains short-lived WebSocket
            # access credentials. Keep production logs at WARNING and emit our
            # own credential-free lifecycle message below.
            log_level=LogLevel.WARNING,
            transport="ws",
            policy=PolicyConfig(
                dm_policy="open",
                group_policy="open",
                require_mention=True,
                respond_to_mention_all=False,
            ),
            # Every Feishu event maps to one durable job. SDK-side merging would
            # destroy the source message id used for idempotency and thread replies.
            safety=SafetyConfig(
                text_batch=TextBatchConfig(
                    delay_ms=0,
                    long_delay_ms=0,
                    max_messages=1,
                    max_chars=10_000,
                ),
                chat_queue=ChatQueueConfig(enabled=False, merge_while_busy=False),
            ),
        )
        self.channel.on(Events.MESSAGE, self._on_message)
        self.channel.on(Events.ERROR, self._on_channel_error)

    async def _on_channel_error(self, error) -> None:
        logger.error("Feishu channel error: %s", type(error).__name__)

    def _wake_worker(self, kind: str) -> None:
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.wake_workers[kind].set)

    async def _on_message(self, message) -> None:
        if bool(getattr(message, "sender_is_bot", False)):
            return
        message_id = str(getattr(message, "message_id", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        text = str(
            getattr(message, "body_text", "")
            or getattr(message, "content_text", "")
            or ""
        ).strip()
        if not message_id or not chat_id or not text:
            logger.info("Ignored an inbound event without text, chat_id, or message_id")
            return
        raw = getattr(message, "raw", {}) or {}
        raw_parent_id = (
            str(raw.get("parent_id", "") or "") if isinstance(raw, Mapping) else ""
        )
        raw_root_id = (
            str(raw.get("root_id", "") or "") if isinstance(raw, Mapping) else ""
        )
        incoming = IncomingMessage(
            message_id=message_id,
            chat_id=chat_id,
            text=text[:10_000],
            chat_type=str(getattr(message, "chat_type", "") or ""),
            sender_open_id=str(getattr(message, "sender_id", "") or ""),
            thread_id=str(
                getattr(message, "thread_id", "")
                or getattr(getattr(message, "conversation", None), "thread_id", "")
                or ""
            ),
            parent_message_id=str(
                getattr(getattr(message, "reply", None), "message_id", "")
                or raw_parent_id
                or raw_root_id
                or ""
            ),
        )
        inserted = await asyncio.to_thread(
            self.store.enqueue,
            f"message:{incoming.message_id}",
            "message",
            incoming.__dict__,
        )
        if inserted:
            self._wake_worker("message")

    def _ensure_reply(
        self,
        job: Job,
        group_key: str,
        markdown: str,
        message_id: str,
        idempotency_key: str,
        reply_in_thread: bool,
        file_keys: tuple[str, ...] = (),
    ) -> None:
        parts = delivery_parts(markdown, idempotency_key)
        for file_key in file_keys:
            parts.append(
                file_delivery_part(file_key, idempotency_key, len(parts) + 1)
            )
        self.store.ensure_outbox(
            job_key=job.key,
            group_key=group_key,
            delivery_key=idempotency_key,
            operation="reply",
            target_id=message_id,
            target_type="",
            reply_in_thread=reply_in_thread,
            parts=parts,
        )

    def _ensure_broadcast(
        self,
        job: Job,
        group_key: str,
        markdown: str,
        idempotency_key: str,
        file_key: str = "",
    ) -> None:
        targets = self._daily_targets(job)
        if not targets:
            raise RuntimeError("A daily delivery has no active subscribers")
        for target_type, target_id in targets:
            delivery_key = f"{idempotency_key}:{target_type}:{target_id}"
            parts = delivery_parts(markdown, delivery_key)
            if file_key:
                parts.append(
                    file_delivery_part(file_key, delivery_key, len(parts) + 1)
                )
            self.store.ensure_outbox(
                job_key=job.key,
                group_key=group_key,
                delivery_key=delivery_key,
                operation="send",
                target_id=target_id,
                target_type=target_type,
                reply_in_thread=False,
                parts=parts,
            )

    def _uploaded_transcript_file(
        self,
        job: Job,
        attachment: TranscriptAttachment,
    ) -> str:
        filename_sha256 = hashlib.sha256(
            attachment.filename.encode("utf-8")
        ).hexdigest()
        result_key = (
            f"transcript-file:{attachment.renderer_version}:"
            f"{attachment.episode_id}:{attachment.source_sha256}:"
            f"{attachment.record_revision_sha256}:"
            f"{attachment.digest_sha256}:{attachment.rendered_sha256}:"
            f"{filename_sha256}"
        )
        persisted = self.store.get_job_result(job.key, result_key)
        if persisted is not None:
            if persisted.get("attachment") != attachment.to_persisted_dict():
                return ""
            return str(persisted["file_key"])
        # A previously uploaded artifact remains safe to replay even after its
        # source row changes. Without such an upload, however, an old renderer
        # or changed input cannot be reconstructed and must fail closed.
        if attachment.renderer_version != RENDERER_VERSION:
            return ""
        record = self.store.get_verified_transcript(attachment.episode_id)
        if record is None or record.content_sha256 != attachment.source_sha256:
            return ""
        if record.record_revision_sha256 != attachment.record_revision_sha256:
            return ""
        revision = self.store.get_transcript_digest_revision(
            attachment.episode_id
        )
        if (
            revision is not None
            and revision[0] == attachment.source_sha256
            and revision[1] == attachment.record_revision_sha256
        ):
            digest_markdown = revision[2]
        else:
            digest_markdown = ""
        digest_sha256 = hashlib.sha256(
            digest_markdown.encode("utf-8")
        ).hexdigest()
        if digest_sha256 != attachment.digest_sha256:
            return ""
        filename, content = render_transcript_attachment(
            record, digest_markdown=digest_markdown
        )
        if filename != attachment.filename:
            return ""
        if hashlib.sha256(content).hexdigest() != attachment.rendered_sha256:
            return ""
        file_key = self.messenger.upload_file(content, filename)
        canonical = self.store.save_job_result(
            job.key,
            result_key,
            "transcript_file",
            {
                "attachment": attachment.to_persisted_dict(),
                "file_key": file_key,
            },
        )
        if canonical.get("attachment") != attachment.to_persisted_dict():
            return ""
        return str(canonical["file_key"])

    def _daily_targets(self, job: Job) -> list[tuple[str, str]]:
        """Freeze one DB-backed recipient snapshot for the whole daily job."""

        persisted = self.store.get_job_result(job.key, "daily:recipients")
        if persisted is None:
            targets = self.store.list_subscriptions()
            persisted = self.store.save_job_result(
                job.key,
                "daily:recipients",
                "delivery_targets",
                {"targets": [list(target) for target in targets]},
            )
        return [
            (str(target[0]), str(target[1]))
            for target in persisted.get("targets", ())
        ]

    async def _drain_outbox(self, job: Job) -> None:
        failures: list[tuple[str, Exception]] = []
        blocked_deliveries: set[str] = set()
        for item in await asyncio.to_thread(self.store.pending_outbox, job.key):
            # Keep parts ordered within one recipient, while allowing a bad
            # recipient to never block every other user/group in the broadcast.
            if item.delivery_key in blocked_deliveries:
                continue
            await asyncio.to_thread(self.store.mark_outbox_attempt, item.id)
            try:
                remote_message_id = await asyncio.to_thread(
                    self.messenger.deliver, item
                )
            except Exception as error:  # noqa: BLE001 - isolate failures per recipient
                await asyncio.to_thread(
                    self.store.mark_outbox_error, item.id, str(error)
                )
                blocked_deliveries.add(item.delivery_key)
                failures.append((item.delivery_key, error))
            else:
                await asyncio.to_thread(
                    self.store.mark_outbox_sent,
                    item.id,
                    str(remote_message_id or ""),
                )
        if failures:
            delivery_key, error = failures[0]
            raise RuntimeError(
                f"{len(failures)} outbox delivery group(s) failed; first={delivery_key}"
            ) from error

    async def _handle_message_job(self, job: Job) -> None:
        payload = job.payload
        message_id = str(payload["message_id"])
        text = str(payload["text"])
        plugin = self.router.select(text)
        reply_in_thread = bool(payload.get("thread_id"))
        acknowledgement = plugin.acknowledgement(text)
        if acknowledgement:
            await asyncio.to_thread(
                self._ensure_reply,
                job,
                "message:ack",
                acknowledgement,
                message_id,
                f"{job.key}:ack",
                reply_in_thread,
            )
            # Acknowledge only after the durable queue insert and before the
            # expensive plugin/model work.
            await self._drain_outbox(job)

        persisted = await asyncio.to_thread(
            self.store.get_job_result, job.key, "message:analysis"
        )
        if persisted is None:
            incoming = IncomingMessage(
                message_id=message_id,
                chat_id=str(payload.get("chat_id") or ""),
                text=text,
                chat_type=str(payload.get("chat_type") or ""),
                sender_open_id=str(payload.get("sender_open_id") or ""),
                thread_id=str(payload.get("thread_id") or ""),
                parent_message_id=str(payload.get("parent_message_id") or ""),
            )
            response = await self._invoke_with_progress(job, plugin, text, incoming)
            attachment_descriptors = [
                descriptor.to_persisted_dict()
                for descriptor in response.attachments
                if descriptor.episode_id in response.attachment_episode_ids
            ]
            persisted = await asyncio.to_thread(
                self.store.save_job_result,
                job.key,
                "message:analysis",
                "message",
                {
                    "messages": list(response.messages),
                    "attachment_episode_ids": list(
                        response.attachment_episode_ids
                    ),
                    "attachments": attachment_descriptors,
                    "context_episode_id": response.context_episode_id,
                },
            )
        attachment_episode_ids = tuple(
            str(item)
            for item in persisted.get("attachment_episode_ids") or ()
        )
        descriptor_by_episode: dict[str, TranscriptAttachment] = {}
        for value in persisted.get("attachments") or ():
            if not isinstance(value, dict):
                continue
            try:
                descriptor = TranscriptAttachment.from_persisted_dict(value)
            except (TypeError, ValueError):
                continue
            if (
                descriptor.episode_id in attachment_episode_ids
                and descriptor.episode_id not in descriptor_by_episode
            ):
                descriptor_by_episode[descriptor.episode_id] = descriptor
        uploaded_file_keys: list[str] = []
        missing_attachment_ids: list[str] = []
        for episode_id in attachment_episode_ids:
            descriptor = descriptor_by_episode.get(episode_id)
            if descriptor is None:
                # Analyses persisted by older versions intentionally do not
                # snapshot today's transcript. That would pair an old reply
                # with a new attachment after a retry.
                missing_attachment_ids.append(episode_id)
                continue
            file_key = await asyncio.to_thread(
                self._uploaded_transcript_file, job, descriptor
            )
            if file_key:
                uploaded_file_keys.append(file_key)
            else:
                missing_attachment_ids.append(episode_id)
        file_keys = tuple(uploaded_file_keys)
        replies = tuple(str(reply) for reply in persisted.get("messages") or ())
        if missing_attachment_ids:
            unavailable = (
                "精编文字稿附件暂不可用：生成时核验的原稿版本已缺失或发生变化。"
                "请重新发送节目链接，待完整文字稿重新核验后再下载。"
            )
            replies = tuple(
                reply.replace(
                    "已附上精编可读版文字稿", "未能附上精编可读版文字稿"
                ).replace("已附上完整文字稿", "未能附上完整文字稿")
                for reply in replies
            )
            if replies:
                replies = (*replies[:-1], f"{replies[-1]}\n\n{unavailable}")
            else:
                replies = (unavailable,)
        if not replies and file_keys:
            replies = ("精编可读版文字稿见附件；原始核验全文已保留用于问答。",)
        context_episode_id = str(persisted.get("context_episode_id") or "")
        for index, reply in enumerate(replies, start=1):
            group_key = (
                f"episode:{context_episode_id}"
                if context_episode_id
                else f"message:result:{index}"
            )
            await asyncio.to_thread(
                self._ensure_reply,
                job,
                group_key,
                reply,
                message_id,
                f"{job.key}:result:{index}",
                reply_in_thread,
                file_keys if index == len(replies) else (),
            )
        await asyncio.to_thread(self.store.mark_analysis_complete, job.key)
        await self._drain_outbox(job)

    def _persisted_daily_items(self, job: Job) -> list[DailyItem]:
        from .daily_report import ranked_daily_items
        values = self.store.list_job_results(job.key, "daily_item")
        return ranked_daily_items([DailyItem.from_persisted_dict(value) for value in values])

    def _persist_daily_items(self, job: Job, results: list[DailyItem]) -> None:
        for item in results:
            if item.status == "failed":
                continue
            if item.status == "summarized":
                descriptor = item.attachment
                if (
                    descriptor is None
                    or descriptor.episode_id != item.episode.id
                    or descriptor.digest_sha256
                    != hashlib.sha256(
                        item.message.strip().encode("utf-8")
                    ).hexdigest()
                ):
                    raise RuntimeError(
                        "A summarized daily episode has no matching immutable "
                        f"transcript revision: {item.episode.id}"
                    )
            canonical = self.store.save_job_result(
                job.key,
                f"episode:{item.episode.id}",
                "daily_item",
                item.to_persisted_dict(),
            )
            # Parse the first immutable value so a concurrent writer can never
            # cause a different payload to be delivered under the same UUID.
            DailyItem.from_persisted_dict(canonical)

    def _prepare_daily_outbox_and_terminal_states(self, job: Job) -> list[DailyItem]:
        items = self._persisted_daily_items(job)
        for item in items:
            if item.status == "summarized":
                if item.attachment is None:
                    self._ensure_broadcast(
                        job,
                        f"attachment-unavailable:{item.episode.id}",
                        (
                            f"《{item.episode.title}》的精编文字稿版本信息已过期，"
                            "本次未发送摘要；系统会在后续扫描中重新核验。"
                        ),
                        f"daily:attachment-unavailable:{item.episode.id}",
                    )
                    self.store.record_episode(
                        item.episode, "summary_format_error"
                    )
                    continue
                with_file = getattr(
                    getattr(self, "settings", None), "daily_transcript_attachments", False
                )
                file_key = (
                    self._uploaded_transcript_file(job, item.attachment) if with_file else ""
                )
                # Text-only delivery still binds the summary to its verified
                # source revision. Not uploading a file must not bypass integrity.
                valid = bool(file_key) if with_file else self._daily_summary_is_current(item)
                if not valid:
                    self._ensure_broadcast(
                        job,
                        f"attachment-unavailable:{item.episode.id}",
                        (
                            f"《{item.episode.title}》的原稿或摘要版本在发送前发生变化，"
                            "本次未发送；系统会在后续扫描中重新核验。"
                        ),
                        f"daily:attachment-unavailable:{item.episode.id}",
                    )
                    self.store.record_episode(
                        item.episode, "summary_format_error"
                    )
                    continue
                # A cached v1 digest can contain numeric scores. Normalize the
                # new delivery only, preserving its archived revision and any
                # already-frozen outbox parts from an interrupted older release.
                from .daily_report import render_daily_summary

                if any(part.group_key == f'episode:{item.episode.id}'
                       for part in self.store.outbox_items(job.key)):
                    continue
                self._ensure_broadcast(
                    job,
                    f"episode:{item.episode.id}",
                    ('全文已补齐｜补充摘要\n\n' if job.payload.get('transcript_catchup') else '')
                    + render_daily_summary(item.message),
                    f"daily:{item.episode.id}",
                    file_key,
                )
            elif item.status in {
                "not_recommended",
                "no_transcript",
                "outside_window",
                "summary_format_error",
                "unverified_date",
            }:
                self.store.record_episode(item.episode, item.status)
        return items

    def _daily_summary_is_current(self, item: DailyItem) -> bool:
        descriptor = item.attachment
        if descriptor is None:
            return False
        record = self.store.get_verified_transcript(item.episode.id)
        revision = self.store.get_transcript_digest_revision(item.episode.id)
        return bool(
            record
            and record.content_sha256 == descriptor.source_sha256
            and record.record_revision_sha256 == descriptor.record_revision_sha256
            and revision
            and revision[0] == descriptor.source_sha256
            and revision[1] == descriptor.record_revision_sha256
            and revision[2] == item.message.strip()
            and hashlib.sha256(item.message.strip().encode()).hexdigest()
            == descriptor.digest_sha256
        )

    def _finalize_daily_deliveries(self, job: Job, items: list[DailyItem]) -> None:
        for item in items:
            if item.status == "summarized" and self.store.outbox_group_sent(
                job.key, f"episode:{item.episode.id}"
            ):
                self.store.record_episode(item.episode, "sent")
                self.store.complete_daily_transcript(item.episode.id)

    async def _handle_daily_job(self, job: Job) -> None:
        targets = await asyncio.to_thread(self._daily_targets, job)
        if not targets:
            # A queued job may race with the last recipient unsubscribing. Do
            # not spend transcript/model capacity when nobody can receive it.
            await asyncio.to_thread(self.store.mark_analysis_complete, job.key)
            return
        # Always finish already-generated immutable deliveries before asking
        # providers or the model for more work.
        persisted = await asyncio.to_thread(
            self._prepare_daily_outbox_and_terminal_states, job
        )
        await self._drain_outbox(job)
        await asyncio.to_thread(self._finalize_daily_deliveries, job, persisted)
        if await asyncio.to_thread(self.store.analysis_complete, job.key):
            return

        try:
            build = (self.podcast_service.build_pending if job.payload.get('transcript_catchup')
                     else self.podcast_service.build_daily)
            results = await asyncio.to_thread(build)
        except Exception:
            # A broken source scan is not an empty digest. Persist and deliver a
            # single idempotent warning while leaving the job retryable.
            await asyncio.to_thread(
                self._ensure_broadcast,
                job,
                "daily:scan-failure",
                (
                    "今日播客扫描暂未完成：节目源或文字稿服务暂时不可访问。"
                    "本次不会判定为“无更新”，系统将自动重试。"
                ),
                f"{job.key}:scan-failure",
            )
            await self._drain_outbox(job)
            raise
        failures = sum(item.status == "failed" for item in results)
        await asyncio.to_thread(self._persist_daily_items, job, results)
        persisted = await asyncio.to_thread(
            self._prepare_daily_outbox_and_terminal_states, job
        )

        if failures:
            await asyncio.to_thread(
                self._ensure_broadcast,
                job,
                "daily:candidate-failure",
                (
                    f"今日有 {failures} 个节目源或候选节目处理失败，扫描结果可能不完整。"
                    "失败项已保留，系统将自动重试。"
                ),
                f"{job.key}:candidate-failure",
            )

        if not failures:
            report = coverage_report(persisted)
            if report and not job.payload.get('transcript_catchup'):
                group = "daily:coverage" if persisted else "daily:empty"
                await asyncio.to_thread(
                    self._ensure_broadcast,
                    job,
                    group,
                    report,
                    f"{job.key}:{group}",
                )
            # This flag is committed before delivery. A crash can therefore
            # only replay the fixed outbox, never regenerate or emit false empty.
            await asyncio.to_thread(self.store.mark_analysis_complete, job.key)

        await self._drain_outbox(job)
        await asyncio.to_thread(self._finalize_daily_deliveries, job, persisted)
        if failures:
            raise RuntimeError(
                f"{failures} podcast candidates failed and remain retryable"
            )

    async def _invoke_with_progress(self, job, plugin, text, incoming):
        # Never stream an unvalidated partial answer. Fast replies stay quiet;
        # slow work gets one durable, idempotent progress notice per event.
        work = asyncio.create_task(asyncio.to_thread(plugin.handle, text, incoming))
        try:
            done, _ = await asyncio.wait({work}, timeout=getattr(getattr(self, 'settings', None), 'progress_delay_seconds', 8))
            if not done and plugin.acknowledgement(text) is None:
                try:
                    await asyncio.to_thread(self._ensure_reply, job, 'message:progress',
                        '这条请求仍在处理中，完成后会在这里回复；不需要重复发送。',
                        incoming.message_id, f'{job.key}:progress', bool(incoming.thread_id))
                    await self._drain_outbox(job)
                except Exception as error:  # noqa: BLE001 - preserve running analysis and retry delivery
                    logger.warning('Progress notice deferred: %s', type(error).__name__)
            return await work
        finally:
            # Do not start a second tool loop after a progress-send failure.
            # Shutdown is handled by the process supervisor and durable jobs.
            if not work.done():
                work.cancel()

    async def _failure_notice(self, job, terminal):
        if job.kind != 'message':
            return
        message_id = job.payload.get('message_id')
        if not message_id:
            return
        stage = 'failed' if terminal else 'retry'
        text = ('这次请求遇到持续故障，自动重试仍未完成。我已保留这条请求，不能把它说成处理成功。你可以问我“检查这次请求的状态”。'
                if terminal else '处理这条请求时连接或服务暂时出错，我正在自动重试；你不需要重新发送问题。')
        try:
            await asyncio.to_thread(self._ensure_reply, job, f'message:{stage}', text,
                message_id, f'{job.key}:{stage}', bool(job.payload.get('thread_id')))
            await self._drain_outbox(job)
        except Exception as error:  # noqa: BLE001 - a notice must not kill the worker
            logger.warning('Failure notice delivery deferred: %s', type(error).__name__)

    async def _worker(self, kind: str) -> None:
        wake_event = self.wake_workers[kind]
        while True:
            # Clear before looking at SQLite. An enqueue that commits after this
            # point sets the event, so its wake-up cannot be erased by the worker.
            wake_event.clear()
            job = await asyncio.to_thread(self.store.claim_next, kind)
            if job is None:
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=10)
                except TimeoutError:
                    pass
                continue
            try:
                if kind == 'message' and hasattr(self, 'active_jobs'):
                    self.active_jobs[job.key] = time.monotonic()
                if kind == "message":
                    await self._handle_message_job(job)
                elif kind == "daily":
                    await self._handle_daily_job(job)
                else:
                    raise ValueError(f"Unknown job kind: {job.kind}")
            except Exception as error:  # noqa: BLE001 - durable worker retry boundary
                logger.error("Job %s failed: %s", job.key, type(error).__name__)
                if not isinstance(error, ResearchContinuationPending) or job.attempts >= 5:
                    await self._failure_notice(job, terminal=job.attempts >= 5)
                await asyncio.to_thread(
                    self.store.fail, job.key, type(error).__name__, job.attempts
                )
            else:
                await asyncio.to_thread(self.store.complete, job.key)
            finally:
                if hasattr(self, 'active_jobs'):
                    self.active_jobs.pop(job.key, None)

    async def _scheduler(self) -> None:
        while True:
            local_now = datetime.now(self.settings.timezone)
            has_subscribers = await asyncio.to_thread(self.store.has_subscriptions)
            if has_subscribers and local_now.time() >= self.settings.daily_time:
                jobs = [
                    (
                        f"daily:{local_now.date().isoformat()}",
                        {"scheduled_for": local_now.isoformat()},
                    )
                ]
                # A one-time backfill is explicit operator state, not coupled
                # to a code version. Keeping the ID stable makes it idempotent
                # across deploys and prevents accidental same-day re-sends.
                manual_digest_id = self.settings.manual_digest_id
                if manual_digest_id:
                    jobs.append(
                        (
                            f"daily:manual:{manual_digest_id}",
                            {
                                "scheduled_for": local_now.isoformat(),
                                "manual_digest_id": manual_digest_id,
                            },
                        )
                    )
                for key, payload in jobs:
                    inserted = await asyncio.to_thread(
                        self.store.enqueue,
                        key,
                        "daily",
                        payload,
                    )
                    if inserted:
                        logger.info("Queued daily digest %s", key)
                        self._wake_worker("daily")
            if has_subscribers and await asyncio.to_thread(self.store.due_daily_transcripts, 1):
                slot = int(local_now.timestamp()) // 1800
                inserted = await asyncio.to_thread(self.store.enqueue,
                    f'daily:transcript-catchup:{slot}', 'daily',
                    {'scheduled_for': local_now.isoformat(), 'transcript_catchup': True})
                if inserted:
                    self._wake_worker('daily')
            await asyncio.sleep(30)

    async def _library_archiver(self) -> None:
        while True:
            try:
                completed = await asyncio.to_thread(self.research_agent.library.archive_pending)
                if completed:
                    logger.info("Verified %s podcast archive(s)", len(completed))
            except Exception as error:  # noqa: BLE001 - independent worker retries without stopping the bot
                logger.warning("Library archive pending retry: %s", type(error).__name__)
            await asyncio.sleep(60)

    async def _channel_loop(self) -> None:
        # connect() runs the SDK's foreground WS loop: it can remain blocked
        # before _mark_ready(). Use its public async readiness API instead.
        await self.channel.connect_until_ready(timeout=60)
        if not self.channel.connection_snapshot().ready:
            raise RuntimeError('feishu-channel stopped before readiness')
        logger.info('Feishu channel is ready')
        # Reconnect/stall detection is handled by the health watchdog. A ready
        # connection is long-lived, not a task that should immediately finish.
        await asyncio.Future()

    async def run(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        self.store.initialize()
        if self.research_agent is not None:
            self.research_agent.initialize()
            logger.info("Research execution: %s (model=%s)", self.research_agent.backend, self.settings.openai_model)
        else:
            logger.warning("Research execution: legacy intent router; Agents SDK mode is disabled")
        seeded = self.store.seed_subscriptions(
            self.settings.user_open_ids, self.settings.group_chat_ids
        )
        if seeded:
            logger.info("Imported %s new daily recipient seed(s)", seeded)
        recovered = self.store.recover_interrupted_jobs()
        if recovered:
            logger.warning("Recovered %s interrupted jobs", recovered)
        tasks = [
            *[asyncio.create_task(self._worker("message"), name=f"message-worker-{i}")
              for i in range(getattr(self.settings, "message_workers", 4))],
            asyncio.create_task(self._worker("daily"), name="daily-worker"),
            asyncio.create_task(self._scheduler(), name="daily-scheduler"),
            asyncio.create_task(self._channel_loop(), name="feishu-channel"),
        ]
        if self.research_agent is not None and (
            self.research_agent.library.folder
            or getattr(self.research_agent.library, "archive_root", None)
        ):
            tasks.append(asyncio.create_task(self._library_archiver(), name="library-archiver"))
        if getattr(self.settings, 'health_port', 0):
            snapshot = lambda: health_snapshot(self.channel, self.active_jobs)
            tasks += [asyncio.create_task(serve_health(self.settings.health_port, snapshot), name='health-http'),
                      asyncio.create_task(watch_health(snapshot), name='health-watchdog')]
        try:
            logger.info("情报官 is connecting to Feishu over WebSocket")
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # More than one task can finish in the same event-loop tick. Surface
            # any real failure before reporting a merely unexpected clean stop.
            for stopped in done:
                error = stopped.exception()
                if error is not None:
                    raise error
            stopped = next(iter(done))
            raise RuntimeError(f"Supervised task stopped unexpectedly: {stopped.get_name()}")
        finally:
            if begin_shutdown := getattr(self, 'on_shutdown', None):
                begin_shutdown()
            for task in tasks:
                task.cancel()
            try:
                await self.channel.disconnect()
            except Exception:
                logger.exception("Feishu channel shutdown failed")
            await asyncio.gather(*tasks, return_exceptions=True)
            self._main_loop = None
