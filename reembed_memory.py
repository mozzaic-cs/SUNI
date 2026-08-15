"""Migrate episodic memories from all-MiniLM-L6-v2 (384-dim) to
nomic-embed-text via Ollama (768-dim).

Run with SUNI stopped to avoid race conditions:
  taskkill /F /IM python.exe
  python reembed_memory.py

Processes every suni_memory.json found under the memory/ directory.
Entries whose e16 field already encodes a 768-dim vector are skipped.
"""
from __future__ import annotations
import asyncio, base64, json, math, os, sys
from pathlib import Path

import httpx
import numpy as np

OLLAMA_HOST  = "http://localhost:11434"
NOMIC_MODEL  = "nomic-embed-text"
TARGET_DIM   = 768
BATCH_SIZE   = 100


def _encode_e16(vec: list[float]) -> str:
    arr = np.asarray(vec, dtype=np.float16)
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _dim_of_e16(b64: str) -> int:
    raw = base64.b64decode(b64)
    return len(raw) // 2  # float16 = 2 bytes per element


async def _embed_batch(texts: list[str], client: httpx.AsyncClient) -> list[list[float]]:
    r = await client.post(
        f"{OLLAMA_HOST}/api/embed",
        json={"model": NOMIC_MODEL, "input": texts},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["embeddings"]


async def migrate_file(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    total = len(data)
    print(f"\n{path} — {total} entries")

    # Identify entries that need re-embedding
    to_embed: list[int] = []
    for i, entry in enumerate(data):
        e16 = entry.get("e16", "")
        if e16 and _dim_of_e16(e16) == TARGET_DIM:
            continue  # already 768-dim
        to_embed.append(i)

    already_done = total - len(to_embed)
    print(f"  {already_done} already {TARGET_DIM}-dim, {len(to_embed)} to re-embed")
    if not to_embed:
        return

    errors = 0
    done   = 0
    async with httpx.AsyncClient() as client:
        for batch_start in range(0, len(to_embed), BATCH_SIZE):
            batch_idx = to_embed[batch_start : batch_start + BATCH_SIZE]
            texts = [data[i]["content"] for i in batch_idx]

            try:
                vecs = await _embed_batch(texts, client)
            except Exception as exc:
                print(f"  [ERROR] batch {batch_start//BATCH_SIZE}: {exc}")
                errors += len(batch_idx)
                continue

            for i_entry, vec in zip(batch_idx, vecs):
                if any(math.isnan(v) or math.isinf(v) for v in vec):
                    errors += 1
                    data[i_entry]["e16"] = _encode_e16([0.0] * TARGET_DIM)
                else:
                    data[i_entry]["e16"] = _encode_e16(vec)
                # Remove old float32 embedding key if present
                data[i_entry].pop("embedding", None)

            done += len(batch_idx)
            if done % 1000 == 0 or done == len(to_embed):
                print(f"  {done}/{len(to_embed)} re-embedded...")

    # Atomic save
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"  Saved. {done - errors} ok, {errors} errors. File: {size_mb:.1f} MB")


async def main() -> None:
    root = Path(__file__).parent / "memory"
    paths = sorted(root.rglob("suni_memory.json"))
    if not paths:
        print("No suni_memory.json files found.")
        sys.exit(0)

    # Verify nomic-embed-text is available
    async with httpx.AsyncClient() as c:
        try:
            r = await c.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
            models = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            print("ERROR: Ollama not reachable at", OLLAMA_HOST)
            sys.exit(1)

    nomic_available = any(NOMIC_MODEL in m for m in models)
    if not nomic_available:
        print(f"ERROR: {NOMIC_MODEL} not available in Ollama. Run: ollama pull {NOMIC_MODEL}")
        print(f"Available: {models}")
        sys.exit(1)

    print(f"Found {len(paths)} store(s) to process:")
    for p in paths:
        print(f"  {p}")

    for path in paths:
        await migrate_file(path)

    print("\nMigration complete.")


if __name__ == "__main__":
    asyncio.run(main())
