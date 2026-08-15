"""
Estimated metrics — parameters, price, total cost of ownership.

These are NOT measurements from a benchmark run; they are derived from model
metadata, live throughput, and GPU power draw. Honest but approximate — the
dashboard badges them "estimated". For a local model there is no API price, so
"price" is the energy cost of generating tokens.
"""
from __future__ import annotations
import asyncio
import re
import subprocess

# Default electricity price (EUR/kWh). Override via suni_config key "kwh_price".
_DEFAULT_KWH = 0.22


async def _param_size(model: str, host: str) -> str | None:
    """Human parameter size string from Ollama /api/show, e.g. '7.6B'."""
    try:
        import ollama
        client = ollama.AsyncClient(host=host)
        info = await client.show(model)
        details = getattr(info, "details", None) or (info.get("details") if isinstance(info, dict) else None)
        if details:
            ps = getattr(details, "parameter_size", None) or (
                details.get("parameter_size") if isinstance(details, dict) else None)
            if ps:
                return str(ps)
    except Exception:
        pass
    # Fallback: parse from the model name (e.g. "qwen2.5:7b").
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", model)
    return f"{m.group(1)}B" if m else None


def _gpu_power_watts() -> float | None:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        vals = [float(x) for x in r.stdout.strip().splitlines() if x.strip()]
        return sum(vals) if vals else None
    except Exception:
        return None


async def estimate(model: str, host: str, live: dict, kwh_price: float | None = None) -> dict:
    """
    Returns {parameters, price, tco} each as {"value":…, "detail":…} or value None
    when it cannot be derived (e.g. no throughput data yet).
    """
    kwh = kwh_price if kwh_price is not None else _DEFAULT_KWH
    params, watts = await asyncio.gather(
        _param_size(model, host), asyncio.to_thread(_gpu_power_watts)
    )

    out: dict = {}
    out["parameters"] = {"value": params, "detail": f"from {model}"} if params else {"value": None}

    tps = live.get("tps")
    if watts and tps and tps > 0:
        # price per 1M tokens = energy(kWh) to generate 1M tokens × price/kWh
        secs_per_1m = 1_000_000 / tps
        kwh_per_1m = (watts / 1000.0) * (secs_per_1m / 3600.0)
        price = kwh_per_1m * kwh
        out["price"] = {"value": round(price, 4),
                        "detail": f"{watts:.0f} W · {tps:.0f} tok/s · {kwh:.2f}/kWh"}

        # TCO per 1000 requests using mean generated tokens/request
        gen_mean = live.get("gen_tok_mean")
        if gen_mean:
            secs_per_req = gen_mean / tps
            kwh_per_req = (watts / 1000.0) * (secs_per_req / 3600.0)
            tco = kwh_per_req * kwh * 1000
            out["tco"] = {"value": round(tco, 4),
                          "detail": f"~{gen_mean:.0f} tok/req · energy only"}
        else:
            out["tco"] = {"value": None, "detail": "no request-size data yet"}
    else:
        reason = "no GPU power reading" if not watts else "no throughput data yet"
        out["price"] = {"value": None, "detail": reason}
        out["tco"] = {"value": None, "detail": reason}
    return out
