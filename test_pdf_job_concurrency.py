"""Dry-run concurrency simulation for the PDF job worker pool."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from models import Announcement
from pdf_job_queue import DONE
from pdf_job_queue import enqueue_pdf_job
from pdf_job_queue import init_pdf_job_db
from pdf_job_queue import queue_counts
from pdf_job_worker import PdfJobRuntime
from pdf_job_worker import PdfJobWorkerConfig
from pdf_job_worker import PdfJobWorkerPool


def test_pdf_job_worker_pool_concurrency() -> None:
    """Verify six independent fake GPT requests run as two waves of three."""

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "pdf_jobs.sqlite3"
        init_pdf_job_db(db_path)
        for index in range(6):
            enqueue_pdf_job(
                Announcement(
                    source="NSE",
                    company_name=f"Fake Company {index + 1}",
                    identifier=f"FAKE{index + 1}",
                    announcement_datetime="2026-06-03",
                    subject="Outcome of Board Meeting",
                    pdf_url=f"https://example.test/fake-{index + 1}.pdf",
                    pdf_path=Path(tmpdir) / f"fake-{index + 1}.pdf",
                ),
                db_path=db_path,
            )

        events: list[dict[str, object]] = []
        request_payloads: list[list[str]] = []
        active_fake_gpt = 0
        max_active_fake_gpt = 0
        lock = threading.Lock()

        def event_sink(event: dict[str, object]) -> None:
            events.append(event)

        def fake_process(job: object, runtime: PdfJobRuntime) -> str:
            nonlocal active_fake_gpt, max_active_fake_gpt
            request_payloads.append([str(getattr(job, "pdf_url"))])
            runtime.log_event("GPT_REQUEST_STARTED", status="PROCESSING", pdfs_per_gpt_request=1)
            with lock:
                active_fake_gpt += 1
                max_active_fake_gpt = max(max_active_fake_gpt, active_fake_gpt)
            time.sleep(5)
            with lock:
                active_fake_gpt -= 1
            runtime.log_event("GPT_REQUEST_FINISHED", status="PROCESSING", pdfs_per_gpt_request=1)
            return DONE

        pool = PdfJobWorkerPool(
            process_job=fake_process,
            db_path=db_path,
            config=PdfJobWorkerConfig(
                max_concurrent_pdf_jobs=3,
                pdfs_per_gpt_request=1,
                retry_limit=0,
                idle_sleep_seconds=0.05,
                heartbeat_interval_seconds=1,
            ),
            event_sink=event_sink,
        )
        started = time.perf_counter()
        pool.start()
        try:
            _wait_for(lambda: queue_counts(db_path).processing == 3, timeout_seconds=2)
            early_counts = queue_counts(db_path)
            assert early_counts.processing == 3
            assert early_counts.queued == 3
            _wait_for(lambda: queue_counts(db_path).done == 6, timeout_seconds=16)
        finally:
            pool.stop(wait=True)

        elapsed = time.perf_counter() - started
        final_counts = queue_counts(db_path)
        heartbeat_count = sum(1 for event in events if event.get("event") == "PDF_WORKER_HEARTBEAT")

        assert final_counts.done == 6
        assert final_counts.failed == 0
        assert max_active_fake_gpt == 3
        assert elapsed < 16, f"expected two ~5s waves, got {elapsed:.2f}s"
        assert heartbeat_count >= 1
        assert len(request_payloads) == 6
        assert all(len(payload) == 1 for payload in request_payloads)


def _wait_for(predicate: object, *, timeout_seconds: float) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not met before timeout")


if __name__ == "__main__":
    test_pdf_job_worker_pool_concurrency()
    print("pdf job concurrency simulation passed")
