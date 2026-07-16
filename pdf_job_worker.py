"""Threaded PDF job workers with a hard concurrent GPT request cap."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from db_manager import DB_PATH
from pdf_job_queue import DONE
from pdf_job_queue import FAILED
from pdf_job_queue import PROCESSING
from pdf_job_queue import QUEUED
from pdf_job_queue import SKIPPED_NON_FINANCIAL_DISCLOSURE
from pdf_job_queue import PdfJob
from pdf_job_queue import claim_next_pdf_job
from pdf_job_queue import log_pdf_job_event
from pdf_job_queue import mark_job_done
from pdf_job_queue import mark_job_failed
from pdf_job_queue import mark_job_skipped
from pdf_job_queue import queue_counts
from pdf_job_queue import requeue_job


class RetryablePdfJobError(RuntimeError):
    """A transient job failure that may safely be attempted once more."""


@dataclass(slots=True)
class PdfJobWorkerConfig:
    """Worker-pool runtime configuration."""

    max_concurrent_pdf_jobs: int = 1
    pdfs_per_gpt_request: int = 1
    retry_limit: int = 1
    idle_sleep_seconds: float = 1.0
    heartbeat_interval_seconds: float = 60.0


class PdfJobRuntime:
    """Per-job logging context exposed to the concrete processor."""

    def __init__(
        self,
        *,
        job: PdfJob,
        worker_id: str,
        db_path: Path,
        active_count: Callable[[], int],
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.job = job
        self.worker_id = worker_id
        self.db_path = db_path
        self._active_count = active_count
        self._event_sink = event_sink

    def log_event(self, event: str, *, status: str = "", elapsed_seconds: float = 0.0, **extra: object) -> None:
        """Log a structured event for this job."""

        payload = {
            "event": event,
            "job_id": self.job.id,
            "unique_key": self.job.unique_key,
            "company_name": self.job.company_name,
            "pdf_url": self.job.pdf_url,
            "exchange": self.job.exchange,
            "worker_id": self.worker_id,
            "status": status or self.job.status,
            "active_gpt_jobs_count": self._active_count(),
            "queued_jobs_count": queue_counts(self.db_path).queued,
            "elapsed_seconds": round(float(elapsed_seconds or 0), 3),
            **extra,
        }
        if self._event_sink:
            self._event_sink(payload)
        log_pdf_job_event(
            event,
            job=self.job,
            worker_id=self.worker_id,
            status=str(payload["status"]),
            active_gpt_jobs_count=int(payload["active_gpt_jobs_count"]),
            queued_jobs_count=int(payload["queued_jobs_count"]),
            elapsed_seconds=float(payload["elapsed_seconds"]),
            db_path=self.db_path,
            extra=extra,
        )


ProcessPdfJob = Callable[[PdfJob, PdfJobRuntime], str]


class PdfJobWorkerPool:
    """Long-running worker pool that processes queued PDFs independently."""

    def __init__(
        self,
        *,
        process_job: ProcessPdfJob,
        db_path: Path = DB_PATH,
        config: PdfJobWorkerConfig | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.process_job = process_job
        self.db_path = db_path
        self.config = config or PdfJobWorkerConfig()
        if self.config.pdfs_per_gpt_request != 1:
            raise ValueError("PDFS_PER_GPT_REQUEST must be 1; batching PDFs in one GPT request is not supported.")
        if self.config.max_concurrent_pdf_jobs < 1:
            raise ValueError("MAX_CONCURRENT_PDF_JOBS must be at least 1.")
        self._event_sink = event_sink
        self._stop_event = threading.Event()
        self._active_lock = threading.Lock()
        self._active_jobs = 0
        self._executor: ThreadPoolExecutor | None = None
        self._futures: list[Future[None]] = []
        self._heartbeat_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start worker and heartbeat threads."""

        if self._executor is not None:
            return
        self._stop_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrent_pdf_jobs,
            thread_name_prefix="pdf-job-worker",
        )
        self._futures = [
            self._executor.submit(self._worker_loop, f"worker-{index}")
            for index in range(1, self.config.max_concurrent_pdf_jobs + 1)
        ]
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="pdf-job-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop(self, *, wait: bool = True) -> None:
        """Request shutdown and optionally wait for in-flight jobs."""

        self._stop_event.set()
        if self._heartbeat_thread and wait:
            self._heartbeat_thread.join(timeout=max(2.0, self.config.idle_sleep_seconds + 1.0))
        if self._executor:
            self._executor.shutdown(wait=wait, cancel_futures=False)
        self._executor = None
        self._futures = []

    def active_count(self) -> int:
        """Return the number of active worker jobs."""

        with self._active_lock:
            return self._active_jobs

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop_event.is_set():
            job = claim_next_pdf_job(self.db_path)
            if job is None:
                time.sleep(self.config.idle_sleep_seconds)
                continue
            started = time.perf_counter()
            with self._active_lock:
                self._active_jobs += 1
                active = self._active_jobs
            runtime = PdfJobRuntime(
                job=job,
                worker_id=worker_id,
                db_path=self.db_path,
                active_count=self.active_count,
                event_sink=self._event_sink,
            )
            runtime.log_event("PDF_PROCESSING_STARTED", status=PROCESSING, active_gpt_jobs_count=active)
            try:
                result_status = self.process_job(job, runtime) or DONE
                elapsed = time.perf_counter() - started
                if result_status == SKIPPED_NON_FINANCIAL_DISCLOSURE:
                    mark_job_skipped(job.id, "non-financial disclosure", db_path=self.db_path)
                    runtime.log_event("PDF_PROCESSING_SKIPPED", status=SKIPPED_NON_FINANCIAL_DISCLOSURE, elapsed_seconds=elapsed)
                else:
                    mark_job_done(job.id, db_path=self.db_path)
                    runtime.log_event("PDF_PROCESSING_DONE", status=DONE, elapsed_seconds=elapsed)
            except Exception as exc:
                elapsed = time.perf_counter() - started
                error = str(exc)
                retryable = isinstance(exc, RetryablePdfJobError)
                if retryable and job.attempt_count <= self.config.retry_limit:
                    requeue_job(job.id, error, db_path=self.db_path)
                    runtime.log_event(
                        "PDF_PROCESSING_FAILED",
                        status=QUEUED,
                        elapsed_seconds=elapsed,
                        error=error,
                        retry_scheduled=True,
                        attempt_count=job.attempt_count,
                    )
                else:
                    mark_job_failed(job.id, error, db_path=self.db_path)
                    runtime.log_event(
                        "PDF_PROCESSING_FAILED",
                        status=FAILED,
                        elapsed_seconds=elapsed,
                        error=error,
                        retry_scheduled=False,
                        attempt_count=job.attempt_count,
                    )
                logging.exception("PDF worker failed job_id=%s unique_key=%s", job.id, job.unique_key)
            finally:
                with self._active_lock:
                    self._active_jobs -= 1

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.config.heartbeat_interval_seconds):
            counts = queue_counts(self.db_path)
            payload = {
                "event": "PDF_WORKER_HEARTBEAT",
                "scraper_alive": True,
                "active_gpt_jobs_count": self.active_count(),
                "queued_jobs_count": counts.queued,
                "done_count": counts.done,
                "failed_count": counts.failed,
                "skipped_count": counts.skipped,
            }
            if self._event_sink:
                self._event_sink(payload)
            logging.info(
                "PDF_WORKER_HEARTBEAT scraper_alive=true active_gpt_jobs_count=%s queued_jobs_count=%s "
                "done_count=%s failed_count=%s skipped_count=%s",
                payload["active_gpt_jobs_count"],
                payload["queued_jobs_count"],
                payload["done_count"],
                payload["failed_count"],
                payload["skipped_count"],
            )
