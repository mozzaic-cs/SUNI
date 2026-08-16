"""CLI entry point. Run from the SUNI directory:  python run.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from suni.main import main

if __name__ == "__main__":
    asyncio.run(main())
