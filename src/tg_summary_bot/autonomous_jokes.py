from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from tg_summary_bot.casino import automatic_casino_trigger
from tg_summary_bot.config import Settings
from tg_summary_bot.joke_awards import JokeSelection, JokeSelectionResult, JokeSelector
from tg_summary_bot.storage import JokeJob, MessageStore, StoredMessage


RETRY_SECONDS = (60, 120, 240, 480, 900, 1800, 3600)
TRANSIENT_SELECTION_ERRORS = frozenset({
    "gpu_busy", "lost_lease", "timeout", "model_error", "qualification_timeout",
    "qualification_model_error", "worker_error",
})


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
        # The first-start eligibility boundary is durable; this delivery boundary
        # is per process. A restart must not replay a burst of old award notices.
        self.announcement_start_at = datetime.now(timezone.utc)
        self._gpu_owned = False
        self._session_calls = 0
        self._session_started = 0.0
        self._run_calls = 0
        self._run_sessions = 0
        self._run_jobs = 0
        self._pending_results: list[tuple[JokeJob, JokeSelectionResult]] = []

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
        # Delivery must not wait behind an unrelated multi-batch GPU selection.
        await self._run_outbox()
        next_run_at = await self.store.get_autonomous_joke_next_run_at()
        if next_run_at and datetime.fromisoformat(next_run_at) > now:
            return
        await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=self.settings.allowed_chat_ids,
            disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
            start_after=self.start_after,
            block_messages=self.settings.autonomous_jokes_block_messages,
            partial_min_messages=self.settings.autonomous_jokes_partial_min_messages,
            max_block_age=self.settings.autonomous_jokes_max_block_age,
            now=now,
        )
        # Do not claim (or consume a model-failure attempt) while foreground work owns the GPU.
        try:
            await asyncio.wait_for(self.gpu_lock.acquire(), timeout=0.01)
        except TimeoutError:
            return
        self._start_gpu_session()
        self._run_calls = 0
        self._run_sessions = 0
        jobs = 0
        started = time.monotonic()
        try:
            await self._run_selection_jobs()
            jobs = self._run_jobs
        finally:
            await self._release_gpu_session()
        if jobs:
            logging.info(
                "Joke run finished jobs=%s model_calls=%s gpu_sessions=%s elapsed_s=%.3f next_run_at=%s",
                jobs, self._run_calls, self._run_sessions, time.monotonic() - started,
                await self.store.get_autonomous_joke_next_run_at(),
            )
            await self._run_outbox()

    def _start_gpu_session(self) -> None:
        self._gpu_owned = True
        self._session_calls = 0
        self._session_started = time.monotonic()

    def _session_full(self) -> bool:
        return (self._session_calls >= getattr(self.settings, "autonomous_jokes_session_max_calls", 8)
                or time.monotonic() - self._session_started >= getattr(
                    self.settings, "autonomous_jokes_session_max_seconds", 120))

    async def _release_gpu_session(self) -> None:
        if self._gpu_owned:
            try:
                if self._session_calls:
                    self._run_sessions += 1
                    await self.selector.unload()
            finally:
                self._gpu_owned = False
                self.gpu_lock.release()
                if self._session_calls:
                    logging.info("Joke GPU session finished model_calls=%s elapsed_s=%.3f",
                                 self._session_calls, time.monotonic() - self._session_started)
        # Award accounting must always happen AFTER releasing the shared GPU lock.
        while self._pending_results:
            job, result = self._pending_results[0]
            await self._finish_job(job, result)
            self._pending_results.pop(0)

    async def _run_selection_jobs(self) -> None:
        self._run_jobs = 0
        claimed_ids: set[int] = set()
        for index in range(getattr(self.settings, "autonomous_jokes_max_jobs_per_run", 4)):
            job = await self.store.claim_due_joke_job(
                worker_id=self.worker_id,
                now=datetime.now(timezone.utc),
                lease_seconds=self.settings.autonomous_jokes_lease_seconds,
                allowed_chat_ids=self.settings.allowed_chat_ids,
                disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
                start_after=self.start_after,
                run_interval=(getattr(self.settings, "autonomous_jokes_run_interval", timedelta(hours=6))
                              if index == 0 else None),
                exclude_job_ids=claimed_ids,
            )
            if job is None:
                break
            self._run_jobs += 1
            claimed_ids.add(job.job_id)
            await self._run_job(job)
            if self._session_full():
                break

    async def _select_batch(self, job: JokeJob, candidates: list[StoredMessage], *, qualification: bool = False) -> JokeSelection:
        if self._session_full():
            await self._release_gpu_session()
            # FIFO admission lets queued user requests run. Keep in-flight tournament
            # progress in this coroutine instead of restarting it when the GPU is busy.
            await asyncio.sleep(0)
            await self.gpu_lock.acquire()
            self._start_gpu_session()
        # The job is fenced before every model call; a stale worker stops immediately.
        # Completed selections await an unlocked commit. Keep their leases alive
        # while reusing this model for another job in the same GPU session.
        for pending_job, _result in self._pending_results:
            await self.store.renew_joke_job_lease(
                job_id=pending_job.job_id, lease_token=pending_job.lease_token,
                now=datetime.now(timezone.utc),
                lease_seconds=self.settings.autonomous_jokes_lease_seconds,
            )
        if not job.lease_token or not await self.store.renew_joke_job_lease(
            job_id=job.job_id,
            lease_token=job.lease_token,
            now=datetime.now(timezone.utc),
            lease_seconds=self.settings.autonomous_jokes_lease_seconds,
        ):
            return JokeSelection(False, None, "", valid=False, error="lost_lease")
        self._session_calls += 1
        self._run_calls += 1
        started = time.monotonic()
        phase = "qualify" if qualification else "select"
        logging.info("Joke model call started job_id=%s chat_id=%s attempt=%s phase=%s candidates=%s",
                     job.job_id, job.chat_id, job.attempt_count, phase, len(candidates))
        try:
            return await (self.selector._qualify(candidates[0]) if qualification else self.selector._select_from(candidates))
        finally:
            logging.info("Joke model call finished job_id=%s phase=%s elapsed_s=%.3f",
                         job.job_id, phase, time.monotonic() - started)

    async def _run_job(self, job: JokeJob) -> None:
        assert job.lease_token
        started = time.monotonic()
        calls_before = self._run_calls
        logging.info("Joke job started job_id=%s chat_id=%s attempt=%s messages=%s validation_failures=%s",
                     job.job_id, job.chat_id, job.attempt_count, job.message_count, job.validation_failures)
        try:
            messages = await self.store.get_joke_job_messages(job.job_id)
            result = await self.selector.choose_messages(
                messages,
                select_batch=lambda candidates: self._select_batch(job, candidates),
                qualify_batch=lambda candidate: self._select_batch(job, [candidate], qualification=True),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.exception("Autonomous joke job failed job_id=%s", job.job_id)
            result = JokeSelectionResult("error", reason="worker_error")
        logging.info(
            "Joke selection completed job_id=%s chat_id=%s attempt=%s status=%s reason=%s "
            "pages=%s rounds=%s model_calls=%s elapsed_s=%.3f",
            job.job_id, job.chat_id, job.attempt_count, result.status,
            result.reason if result.status in {"invalid", "error"} else result.status,
            result.pages, result.tournament_rounds, self._run_calls - calls_before,
            time.monotonic() - started,
        )
        self._pending_results.append((job, result))

    async def _finish_job(self, job: JokeJob, result: JokeSelectionResult) -> None:
        assert job.lease_token
        if result.status in {"invalid", "error"}:
            reason = result.reason or "selector_invalid"
            validation_failure = result.status == "invalid" and reason not in TRANSIENT_SELECTION_ERRORS
            limit = getattr(self.settings, "autonomous_jokes_max_validation_failures", 3)
            next_attempt_at = datetime.now(timezone.utc) + retry_delay(job.attempt_count)
            committed = await self.store.fail_joke_job(
                job_id=job.job_id, lease_token=job.lease_token, error_code=reason,
                next_attempt_at=next_attempt_at, validation_failure=validation_failure,
                max_validation_failures=limit,
            )
            quarantined = validation_failure and job.validation_failures + 1 >= limit
            logging.log(
                logging.WARNING if quarantined else logging.INFO,
                "Joke job deferred job_id=%s chat_id=%s attempt=%s reason=%s validation_failures=%s "
                "quarantined=%s next_attempt_at=%s committed=%s",
                job.job_id, job.chat_id, job.attempt_count, reason,
                job.validation_failures + int(validation_failure), quarantined,
                None if quarantined else next_attempt_at.isoformat(), committed,
            )
            return
        try:
            committed = await self.store.finalize_autonomous_joke_job(
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
            logging.info("Joke job committed job_id=%s chat_id=%s status=%s",
                         job.job_id, job.chat_id, committed.status)
        except Exception:  # noqa: BLE001
            logging.exception("Autonomous joke commitment failed job_id=%s", job.job_id)
            await self.store.retry_joke_job(
                job_id=job.job_id,
                lease_token=job.lease_token,
                error_code="worker_error",
                next_attempt_at=datetime.now(timezone.utc) + retry_delay(job.attempt_count),
            )

    async def _run_outbox(self) -> None:
        # Retire expired notices quickly, but send at most one real action per pass.
        for _ in range(10):
            if not await self._run_outbox_once():
                break

    async def _run_outbox_once(self) -> bool | None:
        # Rollback to shadow mode also pauses work committed by an earlier live run.
        if self.settings.autonomous_jokes_shadow_mode:
            return
        allowed_actions = {"award_leaderboard_refresh", "casino_leaderboard_refresh"}
        if self.settings.autonomous_joke_announce:
            allowed_actions.add("award_notification")
        if self.settings.bot_auto_casino_enabled:
            allowed_actions.add("bot_casino")
        outbox = await self.store.claim_due_joke_outbox(
            worker_id=self.worker_id,
            now=datetime.now(timezone.utc),
            lease_seconds=self.settings.autonomous_jokes_lease_seconds,
            allowed_chat_ids=self.settings.allowed_chat_ids,
            disabled_chat_ids=self.settings.joke_awards_disabled_chat_ids,
            start_after=self.start_after,
            allowed_actions=allowed_actions,
            prioritize_notifications=True,
        )
        if not outbox or not outbox.lease_token:
            return
        # Outbox actions are intentionally best effort. A later retry is safe after a crash.
        try:
            if outbox.action == "award_notification":
                job = await self.store.get_joke_job(outbox.job_id)
                if not job or not job.winner_participant_name or job.winner_source_text is None:
                    raise RuntimeError("missing committed award snapshot")
                now = datetime.now(timezone.utc)
                try:
                    awarded_at = datetime.fromisoformat(job.completed_at or "")
                    if awarded_at.tzinfo is None:
                        awarded_at = awarded_at.replace(tzinfo=timezone.utc)
                    max_age = getattr(self.settings, "autonomous_joke_announcement_max_age", timedelta(hours=24))
                    expired = now - awarded_at > max_age
                    reason = "announcement_expired" if expired else None
                    if (not reason and not getattr(self.settings, "autonomous_joke_announce_backlog", False)
                            and awarded_at < self.announcement_start_at):
                        reason = "announcement_before_worker_start"
                except (TypeError, ValueError):
                    reason = "announcement_missing_award_time"
                if reason:
                    retired = await self.store.complete_joke_outbox(
                        outbox_id=outbox.outbox_id, lease_token=outbox.lease_token,
                        status="skipped", reason=reason,
                    )
                    logging.info("Joke notification retired job_id=%s outbox_id=%s reason=%s committed=%s",
                                 job.job_id, outbox.outbox_id, reason, retired)
                    return retired
                available_at = await self.store.reserve_joke_announcement(
                    chat_id=job.chat_id, outbox_id=outbox.outbox_id, lease_token=outbox.lease_token,
                    now=now, min_interval=getattr(self.settings, "autonomous_joke_announcement_min_interval", timedelta(hours=12)),
                )
                if available_at is not None:
                    await self.store.retry_joke_outbox(
                        outbox_id=outbox.outbox_id, lease_token=outbox.lease_token,
                        error_code="announcement_rate_limited", next_attempt_at=available_at,
                    )
                    logging.info("Joke notification deferred job_id=%s chat_id=%s reason=rate_limit until=%s",
                                 job.job_id, job.chat_id, available_at.isoformat())
                    return True  # Other chats and non-notification actions may proceed.
                awarded_label = awarded_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                sent = await self.bot.send_message(
                    job.chat_id,
                    f"Лучшая шутка: {job.winner_participant_name}: «{job.winner_source_text[:1000]}»\n"
                    f"+10 очков начислено {awarded_label}.",
                )
                await self.store.complete_joke_outbox(
                    outbox_id=outbox.outbox_id, lease_token=outbox.lease_token,
                    telegram_message_id=getattr(sent, "message_id", None),
                )
                logging.info("Joke notification sent job_id=%s outbox_id=%s", job.job_id, outbox.outbox_id)
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
