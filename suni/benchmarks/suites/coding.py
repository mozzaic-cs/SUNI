"""MBPP — basic Python programming. Objective: run the model's function against asserts."""
from __future__ import annotations
import asyncio
import re
import sys
import tempfile
import os
from . import SuiteResult, register

# Indicative bundled subset (not the official 974-problem set).
# Each: (prompt, list-of-assert-tests). Tests reference the required function name.
_ITEMS = [
    ("Write a Python function `min_cost` is not needed. Write a function `is_even(n)` that "
     "returns True if n is even, else False.",
     ["assert is_even(4) == True", "assert is_even(7) == False", "assert is_even(0) == True"]),
    ("Write a function `sum_list(nums)` that returns the sum of a list of numbers.",
     ["assert sum_list([1,2,3]) == 6", "assert sum_list([]) == 0", "assert sum_list([-1,1]) == 0"]),
    ("Write a function `reverse_string(s)` that returns the string reversed.",
     ["assert reverse_string('abc') == 'cba'", "assert reverse_string('') == ''",
      "assert reverse_string('a') == 'a'"]),
    ("Write a function `factorial(n)` that returns n! for n >= 0.",
     ["assert factorial(0) == 1", "assert factorial(5) == 120", "assert factorial(1) == 1"]),
    ("Write a function `count_vowels(s)` that returns the number of vowels (aeiou, "
     "case-insensitive) in the string.",
     ["assert count_vowels('hello') == 2", "assert count_vowels('XYZ') == 0",
      "assert count_vowels('AeI') == 3"]),
    ("Write a function `max_of_three(a, b, c)` that returns the largest of three numbers.",
     ["assert max_of_three(1,2,3) == 3", "assert max_of_three(9,2,3) == 9",
      "assert max_of_three(-1,-2,-3) == -1"]),
    ("Write a function `is_palindrome(s)` that returns True if the string reads the same "
     "forwards and backwards.",
     ["assert is_palindrome('racecar') == True", "assert is_palindrome('abc') == False",
      "assert is_palindrome('') == True"]),
    ("Write a function `fib(n)` that returns the n-th Fibonacci number with fib(0)=0, fib(1)=1.",
     ["assert fib(0) == 0", "assert fib(1) == 1", "assert fib(10) == 55"]),
]

_SYS = ("You are a Python programmer. Write ONLY the requested function inside a single "
        "```python code block. No explanation, no example usage, no prints.")

_CODE_BLOCK = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_code(text: str) -> str:
    m = _CODE_BLOCK.search(text)
    if m:
        return m.group(1).strip()
    # No fenced block — take the raw text if it looks like a def.
    return text.strip() if "def " in text else ""


async def _run_candidate(code: str, tests: list[str]) -> bool:
    """Execute candidate + asserts in an isolated subprocess with a timeout."""
    if not code or "def " not in code:
        return False
    script = code + "\n\n" + "\n".join(tests) + "\nprint('BENCH_OK')\n"
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(script)
            path = f.name
        proc = await asyncio.create_subprocess_exec(
            sys.executable, path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return b"BENCH_OK" in (out or b"")
    except Exception:
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


async def run(gen, ctx) -> SuiteResult:
    items = _ITEMS[: ctx.get("limit") or len(_ITEMS)]
    progress = ctx.get("progress")
    passed, details = 0, []
    for i, (prompt, tests) in enumerate(items):
        r = await gen(prompt, system=_SYS, temperature=0.0, seed=7, num_predict=512)
        code = _extract_code(r.get("text", ""))
        ok = await _run_candidate(code, tests)
        passed += ok
        details.append({"ok": ok, "have_code": bool(code)})
        if progress:
            progress("mbpp", i + 1, len(items))
    n = len(items)
    return SuiteResult(
        suite="mbpp",
        metrics={"mbpp": round(100.0 * passed / n, 1) if n else None},
        n=n, passed=passed, details=details,
        notes="Indicative subset; candidate executed against asserts in a sandboxed subprocess.",
    )


register("mbpp", run)
