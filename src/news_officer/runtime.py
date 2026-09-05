from __future__ import annotations

import asyncio
import logging
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
from .feishu import FeishuMessenger, delivery_parts
from .models import DailyItem, IncomingMessage, Job
from .podcast import PodcastService
from .router import CommandRouter
from .store import Store

logger = logging.getLogger(__name__)


class NewsOfficerRuntime:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        messenger: FeishuMessenger,
        router: CommandRouter,
        podcast_service: PodcastService,
    ):
        self.settings = settings
        self.store = store
        self.messenger = messenger
        self.router = router
        self.podcast_service = podcast_service
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
    ) -> None:
        self.store.ensure_outbox(
            job_key=job.key,
            group_key=group_key,
            delivery_key=idempotency_key,
            operation="reply",
            target_id=message_id,
            target_type="",
            reply_in_thread=reply_in_thread,
            parts=delivery_parts(markdown, idempotency_key),
        )

    def _ensure_broadcast(
        self,
        job: Job,
        group_key: str,
        markdown: str,
        idempotency_key: str,
    ) -> None:
        targets = self._daily_targets(job)
        if not targets:
            raise RuntimeError("A daily delivery has no active subscribers")
        for target_type, target_id in targets:
            delivery_key = f"{idempotency_key}:{target_type}:{target_id}"
            self.store.ensure_outbox(
                job_key=job.key,
                group_key=group_key,
                delivery_key=delivery_key,
                operation="send",
                target_id=target_id,
                target_type=target_type,
                reply_in_thread=False,
                parts=delivery_parts(markdown, delivery_key),
            )

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
                await asyncio.to_thread(self.messenger.deliver, item)
            except Exception as error:  # noqa: BLE001 - isolate failures per recipient
                await asyncio.to_thread(
                    self.store.mark_outbox_error, item.id, str(error)
                )
                blocked_deliveries.add(item.delivery_key)
                failures.append((item.delivery_key, error))
            else:
                await asyncio.to_thread(self.store.mark_outbox_sent, item.id)
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
            )
            response = await asyncio.to_thread(plugin.handle, text, incoming)
            persisted = await asyncio.to_thread(
                self.store.save_job_result,
                job.key,
                "message:analysis",
                "message",
                {"messages": list(response.messages)},
            )
        for index, reply in enumerate(persisted.get("messages") or (), start=1):
            await asyncio.to_thread(
                self._ensure_reply,
                job,
                f"message:result:{index}",
                str(reply),
                message_id,
                f"{job.key}:result:{index}",
                reply_in_thread,
            )
        await asyncio.to_thread(self.store.mark_analysis_complete, job.key)
        await self._drain_outbox(job)

    def _persisted_daily_items(self, job: Job) -> list[DailyItem]:
        values = self.store.list_job_results(job.key, "daily_item")
        return [DailyItem.from_persisted_dict(value) for value in values]

    def _persist_daily_items(self, job: Job, results: list[DailyItem]) -> None:
        for item in results:
            if item.status == "failed":
                continue
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
                self._ensure_broadcast(
                    job,
                    f"episode:{item.episode.id}",
                    item.message,
                    f"daily:{item.episode.id}",
                )
            elif item.status in {
                "no_transcript",
                "outside_window",
                "unverified_date",
            }:
                self.store.record_episode(item.episode, item.status)
        return items

    def _finalize_daily_deliveries(self, job: Job, items: list[DailyItem]) -> None:
        for item in items:
            if item.status == "summarized" and self.store.outbox_group_sent(
                job.key, f"episode:{item.episode.id}"
            ):
                self.store.record_episode(item.episode, "sent")

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

        results = await asyncio.to_thread(self.podcast_service.build_daily)
        failures = sum(item.status == "failed" for item in results)
        await asyncio.to_thread(self._persist_daily_items, job, results)
        persisted = await asyncio.to_thread(
            self._prepare_daily_outbox_and_terminal_states, job
        )

        if not failures:
            had_summary = any(item.status == "summarized" for item in persisted)
            if not had_summary:
                await asyncio.to_thread(
                    self._ensure_broadcast,
                    job,
                    "daily:empty",
                    "今日无可摘要的关键播客更新。",
                    f"{job.key}:empty",
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
                if kind == "message":
                    await self._handle_message_job(job)
                elif kind == "daily":
                    await self._handle_daily_job(job)
                else:
                    raise ValueError(f"Unknown job kind: {job.kind}")
            except Exception as error:
                logger.exception("Job %s failed", job.key)
                await asyncio.to_thread(
                    self.store.fail, job.key, str(error), job.attempts
                )
            else:
                await asyncio.to_thread(self.store.complete, job.key)

    async def _scheduler(self) -> None:
        while True:
            local_now = datetime.now(self.settings.timezone)
            has_subscribers = await asyncio.to_thread(self.store.has_subscriptions)
            if has_subscribers and local_now.time() >= self.settings.daily_time:
                key = f"daily:{local_now.date().isoformat()}"
                inserted = await asyncio.to_thread(
                    self.store.enqueue,
                    key,
                    "daily",
                    {"scheduled_for": local_now.isoformat()},
                )
                if inserted:
                    logger.info("Queued daily digest %s", key)
                    self._wake_worker("daily")
            await asyncio.sleep(30)

    async def run(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        self.store.initialize()
        seeded = self.store.seed_subscriptions(
            self.settings.user_open_ids, self.settings.group_chat_ids
        )
        if seeded:
            logger.info("Imported %s new daily recipient seed(s)", seeded)
        recovered = self.store.recover_interrupted_jobs()
        if recovered:
            logger.warning("Recovered %s interrupted jobs", recovered)
        tasks = [
            asyncio.create_task(self._worker("message"), name="message-worker"),
            asyncio.create_task(self._worker("daily"), name="daily-worker"),
            asyncio.create_task(self._scheduler(), name="daily-scheduler"),
            asyncio.create_task(self.channel.connect(), name="feishu-channel"),
        ]
        try:
            logger.info("新闻官 is connecting to Feishu over WebSocket")
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
            for task in tasks:
                task.cancel()
            try:
                await self.channel.disconnect()
            except Exception:
                logger.exception("Feishu channel shutdown failed")
            await asyncio.gather(*tasks, return_exceptions=True)
            self._main_loop = None
