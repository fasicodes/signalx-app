"""Checks that main.signal_core() returns exactly the verdict, confidence
and SL/TP inputs that main.generate_signal() produces (the track record must
measure the same signal users see). main.py itself can't be imported without
its exchange/DB setup, so the function definitions are loaded on their own
and the heavy display-only libraries are stubbed.

Run from the project root:  python tests/test_signal_core.py
"""
import ast
import os
import sys
import types

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and not n.decorator_list]
module = ast.Module(body=funcs, type_ignores=[])


class _Stub:
    def __init__(self, *a, **k):
        raise RuntimeError("stubbed")


class _Exchange:
    def __getattr__(self, name):
        def f(*a, **k):
            raise RuntimeError("no network in tests")
        return f


ns = {"np": np, "pd": pd, "ta": None, "_HAS_PANDAS_TA": False, "GaussianHMM": _Stub, "exchange": _Exchange(),
      "time": __import__("time"), "math": __import__("math")}
try:
    from sklearn.ensemble import RandomForestClassifier
    ns["RandomForestClassifier"] = RandomForestClassifier
except ImportError:
    ns["RandomForestClassifier"] = _Stub
sys.modules.setdefault("pywt", types.ModuleType("pywt"))
exec(compile(module, "main.py", "exec"), ns)
# display-only channel that needs hmmlearn: replace with a deterministic stand-in
ns["hmm_regime"] = lambda df, n_states=2: {"regime": "Ranging", "state": 0, "state_mean_return_pct": 0.01}
signal_core, generate_signal, quantile_volatility = ns["signal_core"], ns["generate_signal"], ns["quantile_volatility"]

passed = 0
mismatch = 0
for seed in range(40):
    rng = np.random.default_rng(seed)
    drift = rng.uniform(-0.002, 0.002)
    close = 100 * np.cumprod(1 + rng.normal(drift, rng.uniform(0.003, 0.02), 200))
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=200, freq="h"),
        "open": close * (1 + rng.normal(0, 0.001, 200)), "high": close * 1.004, "low": close * 0.996,
        "close": close, "volume": rng.uniform(1, 10, 200),
    })
    core = signal_core(df.copy())
    try:
        full = generate_signal(df.copy(), symbol="BTC/USDT", include_orderbook=False)
    except Exception as e:  # pragma: no cover
        raise SystemExit(f"generate_signal failed in test harness: {type(e).__name__}: {e}")
    ok = (core["verdict"] == full["final_verdict"] and core["confidence"] == full["confidence_pct"]
          and core["bullish_pct"] == full["bullish_pct"] and round(core["price"], 2) == full["last_price"]
          and round(core["extreme_move"] * 100, 2) == full["extreme_volatility_95_pct"])
    if full["final_verdict"] in ("LONG", "SHORT"):
        m = core["extreme_move"]
        sl = core["price"] * (1 - m) if core["verdict"] == "LONG" else core["price"] * (1 + m)
        ok = ok and round(sl, 2) == full["stop_loss"]
    mismatch += 0 if ok else 1
    passed += 1 if ok else 0
verdicts = {signal_core(pd.DataFrame({"close": 100 * np.cumprod(1 + np.random.default_rng(s).normal(0, 0.01, 200))}))["verdict"] for s in range(40)}
print(f"signal_core == generate_signal on {passed}/40 random series; verdicts seen: {sorted(verdicts)}")
assert mismatch == 0, f"{mismatch} mismatches"
print("ALL SIGNAL CORE CHECKS PASSED")
