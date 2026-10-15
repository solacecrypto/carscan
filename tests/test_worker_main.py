from app.worker.main import build_scheduler


def test_scheduler_has_a_single_due_searches_job() -> None:
    """Two jobs calling the same loop ran searches twice at once (max_instances is per job)."""
    scheduler = build_scheduler(30)
    scrape_jobs = [j for j in scheduler.get_jobs() if j.func.__name__ == "run_due_searches"]
    assert len(scrape_jobs) == 1
    assert scrape_jobs[0].max_instances == 1
    assert scrape_jobs[0].next_run_time is not None  # starts right away, then every 5 min
