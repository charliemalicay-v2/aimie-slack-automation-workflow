"""Re-run events stuck in received/retry_pending (e.g. after a crash or AI rate limit).

    python -m scripts.reprocess
Run it by hand, or from cron every few minutes. Safe to run repeatedly.
"""
from app.main import create_app

if __name__ == "__main__":
    app = create_app()
    print(f"reprocessed {app.state.pipeline.reprocess_pending()} event(s)")
