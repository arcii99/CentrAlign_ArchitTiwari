"""`uv run python -m agent.console [--port 8103]`: Nova's console in the browser (see app.py)."""

import argparse

import uvicorn
from dotenv import load_dotenv

from agent.config import ROOT

if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="python -m agent.console")
    p.add_argument("--port", type=int, default=8103)
    args = p.parse_args()
    print(f"Nova's console: http://127.0.0.1:{args.port}")
    uvicorn.run("agent.console.app:app", host="127.0.0.1", port=args.port, log_level="warning")
