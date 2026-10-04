from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from tg_summary_bot.casino import automatic_casino_trigger
from tg_summary_bot.config import Settings
from tg_summary_bot.joke_awards import JokeSelection, JokeSelector
from tg_summary_bot.storage import JokeJob, MessageStore, StoredMessage


RETRY_SECONDS = (60, 120, 240, 480, 900, 1800, 3600)


def retry_delay(attempt_count: int) -> timedelta:
    return timedelta(seconds=RETRY_SECONDS[min(max(attempt_count - 1, 0), len(RETRY_SECONDS) - 1)])


def log_worker_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logging.error(
            "Autonomous joke worker stopped unexpectedly",
            exc_info=(type(error), error, error.__traceback__),
        )


class AutonomousJokeWorker:
    """Durable single-process job worker. SQLite leases make cancellation restart-safe."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: MessageStore,
        selector: JokeSelector,
        bot: Bot,
        gpu_lock: asyncio.Lock,
    ) -> None:
        self.settings = settings
        self.store = store
        self.selector = selector
        self.bot = bot
        self.gpu_lock = gpu_lock
        self.worker_id = uuid.uuid4().hex
        self.start_after: datetime | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self.settings.autonomous_jokes_startup_grace_seconds)
            return
        except TimeoutError:
            pass
        backfill_needed = True
        while not stop_event.is_set():
            try:
                if self.start_after is None:
                    self.start_after = await self.store.get_or_create_autonomous_joke_start_at()
                if backfill_needed:
                    await self.store.backfill_autonomous_joke_inbox(
                        allowed_chat_ids=self.settings.allowed_chat_ids,
                        disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
                        lookback=self.settings.autonomous_jokes_initial_lookback,
                        start_after=self.start_after,
                    )
                    backfill_needed = False
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logging.exception("Autonomous joke worker tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.autonomous_jokes_poll_seconds)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        if self.start_after is None:
            self.start_after = await self.store.get_or_create_autonomous_joke_start_at(now=now)
        await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=self.settings.allowed_chat_ids,
            disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
            start_after=self.start_after,
            block_messages=self.settings.autonomous_jokes_block_messages,
            partial_min_messages=self.settings.autonomous_jokes_partial_min_messages,
            max_block_age=self.settings.autonomous_jokes_max_block_age,
            now=now,
        )
        job = await self.store.claim_due_joke_job(
            worker_id=self.worker_id,
            now=now,
            lease_seconds=self.settings.autonomous_jokes_lease_seconds,
            allowed_chat_ids=self.settings.allowed_chat_ids,
            disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
            start_after=self.start_after,
        )
        if job:
            await self._run_job(job)
        await self._run_outbox()

    async def _select_batch(self, job: JokeJob, candidates: list[StoredMessage]) -> JokeSelection:
        # The job is fenced before every model call; a stale worker stops immediately.
        if not job.lease_token or not await self.store.renew_joke_job_lease(
            job_id=job.job_id,
            lease_token=job.lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=self.settings.autonomous_jokes_lease_seconds,
        ):
            return JokeSelection(False, None, "", valid=False, error="lost_lease")
        try:
            await asyncio.wait_for(self.gpu_lock.acquire(), timeout=0.01)
        except TimeoutError:
            return JokeSelection(False, None, "", valid=False, error="gpu_busy")
        try:
            return await self.selector._select_from(candidates)
        finally:
            try:
                await self.selector.unload()
            finally:
                self.gpu_lock.release()
            await asyncio.sleep(0)

    async def _run_job(self, job: JokeJob) -> None:
        assert job.lease_token
        try:
            messages = await self.store.get_joke_job_messages(job.job_id)
            result = await self.selector.choose_messages(
                messages,
                select_batch=lambda candidates: self._select_batch(job, candidates),
            )
            if result.status == "invalid":
                if job.attempt_count >= 10:
                    logging.error(
                        "Autonomous joke job retry threshold exceeded job_id=%s attempts=%s reason=%s",
                        job.job_id,
                        job.attempt_count,
                        result.reason,
                    )
                await self.store.retry_joke_job(
                    job_id=job.job_id,
                    lease_token=job.lease_token,
                    error_code=result.reason or "selector_invalid",
                    next_attempt_at=datetime.now(timezone.utc) + retry_delay(job.attempt_count),
                )
                return
            await self.store.finalize_autonomous_joke_job(
                job_id=job.job_id,
                lease_token=job.lease_token,
                source_message_id=result.winner.message_id if result.winner else None,
                shadow=self.settings.autonomous_jokes_shadow_mode,
                announce=self.settings.autonomous_joke_announce,
                bot_spin_decision=(
                    "skip" if result.winner and not self.settings.autonomous_jokes_shadow_mode
                    and (
                        not self.settings.bot_auto_casino_enabled
                        or job.chat_id in self.settings.casino_disabled_chat_ids
                    ) else None
                ),
                bot_spin_chance=(
                    self.settings.bot_auto_casino_chance
                    if result.winner and not self.settings.autonomous_jokes_shadow_mode
                    and self.settings.bot_auto_casino_enabled
                    and job.chat_id not in self.settings.casino_disabled_chat_ids else None
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.exception("Autonomous joke job failed job_id=%s", job.job_id)
            await self.store.retry_joke_job(
                job_id=job.job_id,
                lease_token=job.lease_token,
                error_code="worker_error",
                next_attempt_at=datetime.now(timezone.utc) + retry_delay(job.attempt_count),
            )

    async def _run_outbox(self) -> None:
        outbox = await self.store.claim_due_joke_outbox(
            worker_id=self.worker_id,
            now=datetime.now(timezone.utc),
            lease_seconds=self.settings.autonomous_jokes_lease_seconds,
            allowed_chat_ids=self.settings.allowed_chat_ids,
            disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
            start_after=self.start_after,
        )
        if not outbox or not outbox.lease_token:
            return
        # Outbox actions are intentionally best effort. A later retry is safe after a crash.
        try:
            if outbox.action == "award_notification":
                job = await self.store.get_joke_job(outbox.job_id)
                if not job or not job.winner_participant_name or job.winner_source_text is None:
                    raise RuntimeError("missing committed award snapshot")
                sent = await self.bot.send_message(
                    job.chat_id,
                    f"Лучшая шутка: {job.winner_participant_name}: «{job.winner_source_text[:1000]}» (+10 очков)",
                )
                await self.store.complete_joke_outbox(
                    outbox_id=outbox.outbox_id, lease_token=outbox.lease_token,
                    telegram_message_id=getattr(sent, "message_id", None),
                )
            elif outbox.action == "award_leaderboard_refresh":
                from tg_summary_bot.bot import refresh_pinned_leaderboard_for_chat

                job = await self.store.get_joke_job(outbox.job_id)
                if not job:
                    raise RuntimeError("missing award job")
                if not await refresh_pinned_leaderboard_for_chat(bot=self.bot, store=self.store, chat_id=job.chat_id):
                    raise RuntimeError("leaderboard_refresh_failed")
                await self.store.complete_joke_outbox(outbox_id=outbox.outbox_id, lease_token=outbox.lease_token)
            elif outbox.action == "bot_casino":
                job = await self.store.get_joke_job(outbox.job_id)
                if not job:
                    raise RuntimeError("missing casino job")
                if job.chat_id in self.settings.casino_disabled_chat_ids:
                    await self.store.complete_automatic_casino_outbox(
                        outbox_id=outbox.outbox_id,
                        lease_token=outbox.lease_token,
                        job_id=job.job_id,
                        spin_id=None,
                        decision="skip",
                    )
                    return
                from tg_summary_bot.bot import cached_bot_identity

                bot_id, _username, fallback_name = await cached_bot_identity(self.bot)
                aliases = await self.store.get_chat_bot_aliases(job.chat_id)
                name = aliases[0].alias if aliases else fallback_name
                reserve = await self.store.reserve_automatic_casino_spin(
                    job_id=job.job_id,
                    outbox_id=outbox.outbox_id,
                    lease_token=outbox.lease_token,
                    chat_id=job.chat_id,
                    participant_key=f"id:{bot_id}",
                    participant_name=name,
                    trigger=automatic_casino_trigger(job.job_id),
                )
                if reserve.status == "insufficient_balance":
                    return
                if reserve.status in {"identity_conflict", "lost_lease"}:
                    raise RuntimeError(f"automatic_reserve_{reserve.status}")
                if not reserve.spin:
                    raise RuntimeError("automatic casino reserve failed")
                spin = reserve.spin
                if reserve.status.startswith("existing_"):
                    if spin.status == "pending":
                        replay = await self.store.finalize_automatic_casino_spin(
                            outbox_id=outbox.outbox_id, lease_token=outbox.lease_token, job_id=job.job_id,
                            spin_id=spin.spin_id, refund_reason="automatic_recovery",
                        )
                        if replay.spin and replay.spin.status in {"completed", "refunded"}:
                            return
                        raise RuntimeError(f"automatic_pending_replay_{replay.status}")
                    terminal = "completed" if spin.status == "completed" else "refunded"
                    completed = await self.store.complete_automatic_casino_outbox(
                        outbox_id=outbox.outbox_id, lease_token=outbox.lease_token, job_id=job.job_id,
                        spin_id=spin.spin_id, decision=terminal,
                    )
                    if not completed:
                        raise RuntimeError("automatic_outbox_lease_lost")
                    return
                from tg_summary_bot.bot import run_casino_dice_lifecycle

                outcome, terminal = await run_casino_dice_lifecycle(
                    chat_id=job.chat_id,
                    spin_id=spin.spin_id,
                    send_dice=lambda: self.bot.send_dice(chat_id=job.chat_id, emoji="🎰"),
                    settle=lambda dice_message_id, dice_value: self.store.finalize_automatic_casino_spin(
                        outbox_id=outbox.outbox_id, lease_token=outbox.lease_token, job_id=job.job_id,
                        spin_id=spin.spin_id, dice_message_id=dice_message_id, dice_value=dice_value,
                    ),
                    refund=lambda reason: self.store.finalize_automatic_casino_spin(
                        outbox_id=outbox.outbox_id, lease_token=outbox.lease_token, job_id=job.job_id,
                        spin_id=spin.spin_id, refund_reason=reason,
                    ),
                )
                expected_statuses = (
                    {"completed", "already_completed"}
                    if outcome == "completed"
                    else {"refunded", "already_refunded", "completed_conflict"}
                )
                if (
                    outcome == "settlement_unconfirmed"
                    or not terminal
                    or not terminal.spin
                    or terminal.status not in expected_statuses
                ):
                    raise RuntimeError("automatic_settlement_unconfirmed")
                return
            elif outbox.action == "casino_leaderboard_refresh":
                from tg_summary_bot.bot import refresh_pinned_leaderboard_for_chat

                job = await self.store.get_joke_job(outbox.job_id)
                if not job:
                    raise RuntimeError("missing casino job")
                if not await refresh_pinned_leaderboard_for_chat(bot=self.bot, store=self.store, chat_id=job.chat_id):
                    raise RuntimeError("leaderboard_refresh_failed")
                await self.store.complete_joke_outbox(outbox_id=outbox.outbox_id, lease_token=outbox.lease_token)
            else:
                await self.store.complete_joke_outbox(
                    outbox_id=outbox.outbox_id, lease_token=outbox.lease_token, status="skipped"
                )
        except Exception:  # noqa: BLE001
            await self.store.retry_joke_outbox(
                outbox_id=outbox.outbox_id,
                lease_token=outbox.lease_token,
                error_code="telegram_error",
                next_attempt_at=datetime.now(timezone.utc) + retry_delay(outbox.attempt_count),
            )
