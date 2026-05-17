"""
OpenAI API 사용량/비용 로깅.
- 각 호출은 data/api_usage.jsonl 에 한 줄(JSON)로 어펜드.
- 가격은 module 상단 OPENAI_PRICING (USD / 1M tokens) 로 관리.
- summary(days=7) 로 일별·모델별 집계 반환.
"""
import json
import os
import threading
from datetime import datetime, timedelta, date
from collections import defaultdict

USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "api_usage.jsonl")
_io_lock = threading.Lock()

# USD per 1,000,000 tokens (2026-05 기준)
OPENAI_PRICING = {
    "gpt-4o-mini":              {"input": 0.15, "output": 0.60},
    "gpt-4o":                   {"input": 2.50, "output": 10.00},
    "gpt-4.1-mini":             {"input": 0.40, "output": 1.60},
    "gpt-4.1":                  {"input": 2.00, "output": 8.00},
    "text-embedding-3-small":   {"input": 0.02, "output": 0.0},
    "text-embedding-3-large":   {"input": 0.13, "output": 0.0},
}

USD_TO_KRW = 1380.0  # 대략치, 표시용


def _price(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD 비용 계산. 모델이 가격표에 없으면 0."""
    p = OPENAI_PRICING.get(model)
    if not p:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000.0


def log(model: str, kind: str, usage_obj, *, meta: dict | None = None) -> None:
    """
    OpenAI response.usage 를 받아 한 줄 어펜드.
    usage_obj 은 openai SDK 의 CompletionUsage / Usage 객체 또는 dict.
    kind: "classify" | "reply" | "embed" 등 자유.
    """
    if usage_obj is None:
        return
    if hasattr(usage_obj, "model_dump"):
        u = usage_obj.model_dump()
    elif hasattr(usage_obj, "dict"):
        u = usage_obj.dict()
    elif isinstance(usage_obj, dict):
        u = usage_obj
    else:
        u = {
            "prompt_tokens":     getattr(usage_obj, "prompt_tokens", 0),
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0),
            "total_tokens":      getattr(usage_obj, "total_tokens", 0),
        }
    inp = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    cost = _price(model, inp, out)
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "kind": kind,
        "input_tokens": inp,
        "output_tokens": out,
        "cost_usd": round(cost, 6),
    }
    if meta:
        row["meta"] = meta
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with _io_lock:
            with open(USAGE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        # 로깅 실패가 본 작업을 막지 않도록 silent
        pass


def _iter_rows():
    if not os.path.exists(USAGE_FILE):
        return
    with open(USAGE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def summary(days: int = 7) -> dict:
    """
    days 일 동안의 사용량/비용 집계.
    반환:
      {
        "range":  {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD", "days": N},
        "total":  {"calls", "input_tokens", "output_tokens", "cost_usd", "cost_krw"},
        "by_day": [{"date","calls","input_tokens","output_tokens","cost_usd"}, ...],  # 최신순
        "by_model":[{"model","calls","input_tokens","output_tokens","cost_usd"}, ...],
        "by_kind": [{"kind","calls","cost_usd"}, ...]
      }
    """
    today = date.today()
    start = today - timedelta(days=days - 1)
    daily   = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    by_model = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    by_kind  = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    tot_calls = tot_in = tot_out = 0
    tot_cost = 0.0

    for row in _iter_rows():
        ts = row.get("ts", "")
        if len(ts) < 10:
            continue
        d_str = ts[:10]
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < start or d > today:
            continue
        inp = int(row.get("input_tokens") or 0)
        out = int(row.get("output_tokens") or 0)
        cost = float(row.get("cost_usd") or 0.0)
        model = row.get("model", "unknown")
        kind  = row.get("kind", "unknown")

        daily[d_str]["calls"]         += 1
        daily[d_str]["input_tokens"]  += inp
        daily[d_str]["output_tokens"] += out
        daily[d_str]["cost_usd"]      += cost

        by_model[model]["calls"]         += 1
        by_model[model]["input_tokens"]  += inp
        by_model[model]["output_tokens"] += out
        by_model[model]["cost_usd"]      += cost

        by_kind[kind]["calls"]    += 1
        by_kind[kind]["cost_usd"] += cost

        tot_calls += 1
        tot_in    += inp
        tot_out   += out
        tot_cost  += cost

    # 날짜별 결과: 범위 전체를 채워 0 인 날도 표시
    by_day = []
    for i in range(days):
        d = start + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        v = daily.get(ds, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        by_day.append({
            "date":           ds,
            "calls":          v["calls"],
            "input_tokens":   v["input_tokens"],
            "output_tokens":  v["output_tokens"],
            "cost_usd":       round(v["cost_usd"], 6),
        })
    by_day.reverse()  # 최신부터

    by_model_list = sorted(
        [{"model": m, **{k: (round(v[k], 6) if k == "cost_usd" else v[k]) for k in v}} for m, v in by_model.items()],
        key=lambda x: -x["cost_usd"],
    )
    by_kind_list = sorted(
        [{"kind": k, "calls": v["calls"], "cost_usd": round(v["cost_usd"], 6)} for k, v in by_kind.items()],
        key=lambda x: -x["cost_usd"],
    )

    return {
        "range": {"from": start.strftime("%Y-%m-%d"), "to": today.strftime("%Y-%m-%d"), "days": days},
        "total": {
            "calls":          tot_calls,
            "input_tokens":   tot_in,
            "output_tokens":  tot_out,
            "cost_usd":       round(tot_cost, 6),
            "cost_krw":       round(tot_cost * USD_TO_KRW),
        },
        "by_day":   by_day,
        "by_model": by_model_list,
        "by_kind":  by_kind_list,
    }
