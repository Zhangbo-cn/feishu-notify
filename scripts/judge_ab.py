#!/usr/bin/env python
"""裁判 A/B 配对分析: 同一批冻结上下文, 两个 judge 的 CP/CR 差多少。

为什么要配对: judge 自身抖动极大 (实测 deepseek 同一上下文跑两遍 CP 0.40→0.56),
跨轮比较两个裁判的绝对分数分不出是裁判差异还是抽样噪声。冻结上下文 + 逐题做差把
"检索侧"这一项彻底消掉, 剩下的差就是裁判偏置。

用法:
  .venv/bin/python scripts/judge_ab.py A.json B.json \
      --metrics context_precision,context_recall --label-a deepseek --label-b opus

A 为基准 (通常 deepseek), B 为对照 (通常 opus)。只读, 输出文本表 + JSON。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load(path: str) -> tuple[dict, dict]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    per_case = {r["case_id"]: r for r in (d.get("per_case") or []) if r.get("case_id")}
    if not per_case:
        raise SystemExit(f"{path}: 没有 per_case, 无法配对")
    return d, per_case


def ctx_src(d: dict) -> str:
    return str((d.get("inputs") or {}).get("contexts_source") or "?")


def boot(deltas: list[float], n: int = 4000, seed: int = 11) -> tuple[float, float]:
    """均值差的 bootstrap 95% CI (逐题做差, 配对)。"""
    rnd = random.Random(seed)
    k = len(deltas)
    means = []
    for _ in range(n):
        means.append(sum(deltas[rnd.randrange(k)] for _ in range(k)) / k)
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n)]


def q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))
    return xs[i]


def slice_report(name: str, keys: list[str], deltas: dict[str, float],
                 meta: dict[str, dict]) -> None:
    """按 key 分组看裁判偏置是否只在某个切片上出现 (语言/模块)。"""
    groups: dict[str, list[float]] = {}
    for cid, dv in deltas.items():
        m = meta.get(cid) or {}
        tag = " / ".join(str(m.get(k) or "?") for k in keys)
        groups.setdefault(tag, []).append(dv)
    rows = [(t, len(v), st.mean(v)) for t, v in groups.items() if len(v) >= 8]
    rows.sort(key=lambda r: -abs(r[2]))
    if not rows:
        return
    print(f"\n  [{name}] 切片 (每组样本 >=8):")
    for t, n, mu in rows[:8]:
        print(f"    {t:<28} n={n:<4} Δ均值={mu:+.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="基准报告 json (如 deepseek)")
    ap.add_argument("b", help="对照报告 json (如 opus)")
    ap.add_argument("--metrics", default="context_precision,context_recall")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--boot", type=int, default=4000)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    da, pa = load(a.a)
    db, pb = load(a.b)
    metrics = [m.strip() for m in a.metrics.split(",") if m.strip()]

    print(f"A = {a.label_a:<12} {a.a}")
    print(f"    judge={da['inputs'].get('judge_model')} 上下文={ctx_src(da)} "
          f"judge失败={da['inputs'].get('judge_failures')}")
    print(f"B = {a.label_b:<12} {a.b}")
    print(f"    judge={db['inputs'].get('judge_model')} 上下文={ctx_src(db)} "
          f"judge失败={db['inputs'].get('judge_failures')}")
    if ctx_src(da) != ctx_src(db) or ctx_src(da) in ("?", "live-service"):
        print("!! 两轮的上下文来源不一致 (或不是同一份冻结文件) —— 配对不成立, 结论无效")

    result = {"a": a.a, "b": a.b, "label_a": a.label_a, "label_b": a.label_b,
              "contexts_source": ctx_src(da), "metrics": {}}
    for mname in metrics:
        both, only_a, only_b = {}, [], []
        for cid in sorted(set(pa) | set(pb)):
            va = (pa.get(cid) or {}).get(mname)
            vb = (pb.get(cid) or {}).get(mname)
            if va is not None and vb is not None:
                both[cid] = (float(va), float(vb))
            elif va is not None:
                only_a.append(cid)
            elif vb is not None:
                only_b.append(cid)
        if not both:
            print(f"\n=== {mname}: 无可配对样本")
            continue
        va_list = [v[0] for v in both.values()]
        vb_list = [v[1] for v in both.values()]
        deltas = {cid: v[1] - v[0] for cid, v in both.items()}
        dl = list(deltas.values())
        lo, hi = boot(dl, a.boot)
        up = sum(1 for d in dl if d > 0.001)
        dn = sum(1 for d in dl if d < -0.001)
        eq = len(dl) - up - dn
        verdict = ("显著 (CI 不跨 0)" if lo > 0 or hi < 0 else "不显著 (CI 跨 0)")
        if abs(st.mean(dl)) < 0.02:
            verdict += " — 且 |Δ|<0.02, 按 v1.3 §6.2 不判"
        print(f"\n=== {mname}  配对 n={len(both)}"
              f" (仅 A 有分 {len(only_a)} / 仅 B 有分 {len(only_b)})")
        print(f"  {a.label_a} 均值={st.mean(va_list):.4f}   "
              f"{a.label_b} 均值={st.mean(vb_list):.4f}")
        print(f"  Δ({a.label_b}-{a.label_a}) 均值={st.mean(dl):+.4f} "
              f"95%CI=[{lo:+.4f},{hi:+.4f}]  -> {verdict}")
        print(f"  中位={st.median(dl):+.4f} p10={q(dl,0.10):+.4f} p90={q(dl,0.90):+.4f}")
        print(f"  逐题方向: B 更高 {up} | B 更低 {dn} | 持平 {eq}")
        try:
            rho = st.correlation(va_list, vb_list)
            print(f"  两裁判逐题相关 r={rho:.3f}")
        except Exception:
            rho = None
        slice_report("language", ["language"], deltas,
                     {c: pb.get(c, {}) for c in deltas})
        slice_report("module", ["module"], deltas, {c: pb.get(c, {}) for c in deltas})
        result["metrics"][mname] = {
            "n_pairs": len(both), "only_a": len(only_a), "only_b": len(only_b),
            "mean_a": round(st.mean(va_list), 4), "mean_b": round(st.mean(vb_list), 4),
            "delta_mean": round(st.mean(dl), 4), "ci95": [round(lo, 4), round(hi, 4)],
            "delta_median": round(st.median(dl), 4),
            "b_higher": up, "b_lower": dn, "tie": eq,
            "corr": round(rho, 3) if rho is not None else None,
            "verdict": verdict,
        }
    result["reported_metrics"] = {"a": da["summary"].get("metrics"),
                                  "b": db["summary"].get("metrics")}
    print(f"\n报告口径 (全量, 仅作参照): A={da['summary'].get('metrics')}")
    print(f"                        B={db['summary'].get('metrics')}")
    out = Path(a.out) if a.out else REPO / "data/eval/ragas" / (
        f"judge_ab_{Path(a.a).stem[:18]}_vs_{Path(a.b).stem[:18]}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
