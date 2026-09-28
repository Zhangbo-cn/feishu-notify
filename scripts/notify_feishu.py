#!/usr/bin/env python
"""跑批守候器: 等跑批结束 -> 配对分析 -> 把结论发飞书。

为什么要守候器: SSH 会话一断, 前台进程会被连带杀掉; 而通知的价值只在"结束时送达"。

三种结局都会通知 (不会静默失效):
  · 正常结束 (--wait-file 出现)      -> 跑 judge_ab.py 配对分析, 发 Δ 与 95%CI
  · 超时     (--wait-timeout)        -> 发"未完成" + 两条臂的当前进度
  · 假死     (--idle-timeout/--pid)  -> 日志长时间不动 或 进程已不在 -> 立刻发"异常" + 进度
                                         (跑批被 OOM 杀掉但没写完成标记时, 不用干等 6 小时)
找不到报告路径时也会发"异常"并把路径贴出来, 方便人工接手。

用法 (必须后台起 + 日志重定向 + </dev/null, 否则 SSH 客户端会假超时):
  nohup .venv/bin/python scripts/notify_feishu.py \
    --wait-file /tmp/ab_arms_DONE \
    --log-a /tmp/arm_a.log --log-b /tmp/arm_b.log --label-a A --label-b B \
    </dev/null > /tmp/notify.log 2>&1 &
  ps -ef | grep notify_feishu       # 确认在跑

exit: 0 正常发出 / 1 未完成或假死 / 2 找不到报告 / 3 分析没产出 / 4 没通道 / 5 参数不全
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feishu_send import (  # noqa: E402  发信/签名/token 只有一份实现
    DEFAULT_CHAT_FILE, FeishuError, deliver, load_app, load_chat, load_cred)

REPO = Path(__file__).resolve().parent.parent


def try_send(text: str, w: str, sec: str, rid: str, app: dict,
             app_name: str = "") -> str:
    """守候器不该因为"发不出去"而默默死掉 —— 失败也要有可读回执。"""
    try:
        desc, r = deliver(text, webhook=w, secret=sec, rid=rid, app=app,
                          app_name=app_name, allow_bot=not rid)
        return f"[{desc}] {r}"
    except FeishuError as e:
        return f"(发送失败: {e})"


def report_from_log(log: str) -> str:
    """跑批日志末尾有 `report=/path/ragas_report_xxx.json`。"""
    p = ""
    if not log or not Path(log).exists():
        return ""
    for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
        if "report=" in line and "ragas_report" in line:
            p = line.split("report=", 1)[1].strip()
    return p


def glob_new_reports(t0: float, want: int = 2) -> list[str]:
    """兜底: 跑批日志里没留下 report= 时, 按修改时间取最新的几个报告。

    只在"恰好 want 个新报告"时才敢用 —— 多了分不清哪条臂是哪条, 宁愿报异常让人接手。
    """
    fs = [p for p in glob.glob(str(REPO / "data/eval/ragas/ragas_report_*.json"))
          if os.path.getmtime(p) >= t0 - 60]
    fs.sort(key=os.path.getmtime, reverse=True)
    return fs if len(fs) == want else []


def stuck(log: str, idle: float = 0) -> str:
    """没跑完时给出进度 (tqdm 用 \\r 刷新, 所以要切行) + 日志停多久了。"""
    if not log or not Path(log).exists():
        return "(日志不存在)"
    txt = Path(log).read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
    last = [l for l in txt.splitlines() if "Evaluating:" in l]
    fails = txt.count("Exception raised in Job")
    ago = time.time() - os.path.getmtime(log)
    return ((last[-1].strip() if last else "(还没有进度)")
            + f" | judge失败={fails} | 日志 {ago / 60:.0f} 分钟没动")


def alive(pid: str) -> bool:
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def deduped(key: str, ttl: int) -> bool:
    """同一件事在 ttl 秒内已发过就返回 True (防止守候器被起两遍/重跑刷屏)。"""
    import hashlib
    f = Path("/tmp") / ("feishu_sent_" + hashlib.md5(key.encode()).hexdigest()[:10])
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return True
    f.write_text(str(time.time()))
    return False


def build_msg(pa: str, pb: str, label_a: str, label_b: str, out_json: str,
              note: str = "") -> str:
    da = json.loads(Path(pa).read_text(encoding="utf-8"))
    db = json.loads(Path(pb).read_text(encoding="utf-8"))
    res = json.loads(Path(out_json).read_text(encoding="utf-8")) if os.path.exists(out_json) else {}
    m = res.get("metrics") or {}
    lines = [f"【pi · 判官 A/B 完成】{label_a} vs {label_b}",
             f"同一批冻结上下文 · 配对 n={next(iter(m.values()), {}).get('n_pairs', '?')}",
             f"裁判: {label_a}={da['inputs'].get('judge_model')} / "
             f"{label_b}={db['inputs'].get('judge_model')}",
             f"裁判失败: {label_a}={da['inputs'].get('judge_failures')} / "
             f"{label_b}={db['inputs'].get('judge_failures')}", ""]
    for name, v in m.items():
        lines.append(f"{name}: {label_a} {v['mean_a']:.4f} → {label_b} {v['mean_b']:.4f} "
                     f"Δ={v['delta_mean']:+.4f} CI[{v['ci95'][0]:+.3f},{v['ci95'][1]:+.3f}]")
        lines.append(f"  逐题: B高 {v['b_higher']} / B低 {v['b_lower']} / 平 {v['tie']} | {v['verdict']}")
    lines += ["", f"{label_a} 全量: {da['summary'].get('metrics')}",
              f"{label_b} 全量: {db['summary'].get('metrics')}"]
    for lb, p in ((label_a, pa), (label_b, pb)):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        lines.append(f"{lb}: cases={d['summary'].get('cases')} scored={d['summary'].get('scored')} "
                     f"failed={d['summary'].get('failed')}")
    lines += ["", f"报告: {Path(pb).name}"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="跑批守候器: 结束/超时/假死都会发飞书")
    ap.add_argument("--wait-file", default="", help="等这个文件出现 (如 /tmp/ab_arms_DONE)")
    ap.add_argument("--wait-timeout", type=int, default=6 * 3600)
    ap.add_argument("--idle-timeout", type=int, default=1800,
                    help="日志多久不更新就判假死 (0=关掉)")
    ap.add_argument("--grace", type=int, default=180,
                    help="假死信号持续这么久才报 (防误判: 刚跑完时进程/日志本来就会停)")
    ap.add_argument("--pid-a", default="", help="臂 A 的 pid (判进程是否已死)")
    ap.add_argument("--pid-b", default="")
    ap.add_argument("--log-a", default="")
    ap.add_argument("--log-b", default="")
    ap.add_argument("--report-a", default="", help="直接给报告路径 (优先于从日志里找)")
    ap.add_argument("--report-b", default="")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--out-json", default="/tmp/judge_ab_result.json")
    ap.add_argument("--to", default="", help="收件人 oc_/ou_/邮箱 (默认读 .feishu_chat)")
    ap.add_argument("--chat-id", default="", help="--to 的同义")
    ap.add_argument("--app", default="", help="用哪个 app (见 .feishu_apps.json)")
    ap.add_argument("--dedup-key", default="", help="同 key 在 --dedup-ttl 秒内只发一次")
    ap.add_argument("--dedup-ttl", type=int, default=1800)
    ap.add_argument("--dry", action="store_true", help="只打印不发")
    ap.add_argument("--text", default="", help="不分析, 直接发这条文本 (验证通道)")
    a = ap.parse_args()

    w, sec = load_cred()
    app_name, app = load_app(a.app)
    rid = load_chat(a.to or a.chat_id, app)
    if not rid and not w:
        print(f"没有可用通道: 写收件人到 {DEFAULT_CHAT_FILE} 并配 .feishu_apps.json, "
              f"或写 webhook 到 .feishu_webhook", file=sys.stderr)
        return 4
    send = (lambda msg: try_send(msg, w, sec, rid, app, app_name)) if not a.dry else \
           (lambda msg: f"(--dry) 通道={'自建应用(' + (app_name or '?') + ')→' + rid if rid else 'webhook'}")

    if a.text:
        print(send(a.text))
        return 0

    t0 = time.time()
    if a.wait_file:
        bad_since = 0.0
        while not os.path.exists(a.wait_file) and time.time() - t0 < a.wait_timeout:
            # 假死: 进程没了 或 两条日志都长时间不动 -> 报, 不干等 6 小时
            spec = [x for x in (a.pid_a, a.pid_b) if x]        # 只判"给定了的"pid
            dead = bool(spec) and all(not alive(x) for x in spec)
            logs = [p for p in (a.log_a, a.log_b) if p and Path(p).exists()]
            idle = (time.time() - max(os.path.getmtime(p) for p in logs)) if logs else 0
            why = ("跑批进程已不在" if dead else
                   (f"日志 {idle / 60:.0f} 分钟没更新"
                    if (a.idle_timeout and logs and idle > a.idle_timeout) else ""))
            if not why:
                bad_since = 0.0                            # 信号消失, 重新计
            elif not bad_since:
                bad_since = time.time()                    # 刚出现: 先观察 grace 秒再说
            elif time.time() - bad_since > a.grace:
                msg = (f"【判官 A/B 异常】{why}, 但 {a.wait_file} 还没出现\n"
                       f"{a.label_a}: {stuck(a.log_a)}\n{a.label_b}: {stuck(a.log_b)}\n"
                       f"→ 人工接手: 看日志尾部 / 重跑该臂")
                print(msg, file=sys.stderr)
                print(send(msg))
                return 1
            time.sleep(30)
        if not os.path.exists(a.wait_file):
            msg = (f"【判官 A/B 未完成】等 {a.wait_file} 超时 ({a.wait_timeout}s)\n"
                   f"{a.label_a}: {stuck(a.log_a)}\n{a.label_b}: {stuck(a.log_b)}")
            print(msg, file=sys.stderr)
            print(send(msg))
            return 1

    if not (a.log_a and a.log_b) and not (a.report_a and a.report_b):
        print("需要 --log-a/--log-b 或 --report-a/--report-b", file=sys.stderr)
        return 5
    note = ""
    pa, pb = (a.report_a or report_from_log(a.log_a)), (a.report_b or report_from_log(a.log_b))
    if not (pa and pb):
        g = glob_new_reports(t0)                      # 兜底: 日志没写 report= 时按时间取最新
        if g and not (pa or pb):
            pb, pa = g[0], g[1]                       # 新的当 B —— 但哪个是 A 无法确定
            note = ("⚠️ 报告路径来自“按修改时间取最新”(日志里没写 report=), "
                    "A/B 谁是哪个不保证, Δ 的符号可能反, 请看上面的裁判名核对")
        else:
            msg = (f"【判官 A/B 异常】找不到结果报告, 跑批可能没跑完/被杀\n"
                   f"{a.label_a}: {stuck(a.log_a)}\n{a.label_b}: {stuck(a.log_b)}\n"
                   f"报告路径 A={pa or '(无)'} B={pb or '(无)'}")
            print(msg, file=sys.stderr)
            print(send(msg))
            return 2
    subprocess.run([sys.executable, str(REPO / "scripts/judge_ab.py"), pa, pb,
                    "--label-a", a.label_a, "--label-b", a.label_b,
                    "--out", a.out_json], check=False)
    if not os.path.exists(a.out_json):
        print("配对分析没产出 json", file=sys.stderr)
        return 3
    text = build_msg(pa, pb, a.label_a, a.label_b, a.out_json, note)
    print(text)
    if a.dry:
        print("\n(--dry 不发送)")
        return 0
    if a.dedup_key and deduped(a.dedup_key, a.dedup_ttl):
        print(f"(跳过: dedup-key={a.dedup_key} 在 {a.dedup_ttl}s 内已发过)")
        return 0
    if not a.dedup_key and deduped(f"ab_done:{Path(pa).name}:{Path(pb).name}", a.dedup_ttl):
        print("(跳过: 这一对报告的结果刚发过)")
        return 0
    print(send(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
