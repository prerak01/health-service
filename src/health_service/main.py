"""Application and command-line entry point for the health service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI


def create_app(*, test_run: bool = False) -> FastAPI:
    """Create the application with its process-level configuration."""
    app = FastAPI(title="Health Service")

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        """Report process health and whether this is a test run."""
        return {"status": "ok", "test_run": test_run}

    return app


app = create_app()


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line configuration."""
    parser = argparse.ArgumentParser(description="Run the health service.")
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="Report test_run=true from the health endpoint.",
    )
    return parser.parse_args(args)


def run(args: Sequence[str] | None = None) -> None:
    """Start the HTTP service."""
    options = parse_args(args) 
    uvicorn.run(create_app(test_run=options.test_run), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
