#!/usr/bin/env python
"""服务器启动/健康报告 -> 飞书。给 @reboot 用, 也可以随时手动跑。

解决一个真实的盲区: 服务器夜里重启 -> 挂着的跑批和守候器全没了, 而你什么都不知道。
这条报告会告诉你: 什么时候起的、docker/检索服务起没起、常驻守护在不在、
以及"有没有跑批被打断"(看 /tmp 里的痕迹)。

默认**只打印不发** (cron 里显式加 --send)。幂等: 同一次启动 10 分钟内只发一条。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feishu_send import load_app, load_chat, load_cred  # noqa: E402
from notify_feishu import try_send  # noqa: E402  发信只有一份实现

REPO = Path(__file__).resolve().parent.parent


def sh(cmd: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip()
    except Exception as e:
        return f"(执行失败: {type(e).__name__})"


def uptime_sec() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return -1.0


def report() -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    up = uptime_sec()
    fresh_boot = 0 <= up < 600
    title = "服务器重启" if fresh_boot else "服务器状态"
    hh = time.strftime("%Y-%m-%d %H:%M:%S")
    up_s = f"{up / 60:.0f} 分钟" if up >= 0 else "?"
    lines = [f"时间: {hh}   已运行: {up_s}"]

    names = sh(["docker", "ps", "--format", "{{.Names}}"]).split()
    want = ["rag-service", "milvus-standalone", "milvus-etcd", "milvus-minio"]
    lines.append("容器: " + " ".join(f"{w}{'✓' if w in names else '✗'}" for w in want)
                 + f"  (共 {len(names)} 个)")
    code = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
               "http://127.0.0.1:8000/readyz"])
    lines.append(f"检索服务 readyz: {code or '无响应'}")

    ps = sh(["ps", "-eo", "pid,etime,args"])
    keep = []
    for pat, label in (("gpu_lease.sh watch", "GPU租约守护"),
                       ("notify_feishu.py", "跑批守候器"),
                       ("ragas_eval_demo.py", "跑批")):  # noqa: E501
        hits = [l for l in ps.splitlines() if pat in l and "grep" not in l]
        keep.append(f"{label}{'✓' if hits else '✗'}"
                    + (f"({hits[0].split()[1]})" if hits else ""))
    lines.append("常驻: " + " ".join(keep))

    # 跑批痕迹: 24h 内的臂日志 + 完成标记, 用来判断"上次跑批是不是被打断"
    arms = sorted(p for p in Path("/tmp").glob("arm_*.log")
                  if time.time() - p.stat().st_mtime < 86400)
    done = [p for p in Path("/tmp").glob("*_DONE") if time.time() - p.stat().st_mtime < 86400]
    if arms:
        unfinished = [p.name for p in arms if not any(
            p.stem.split("arm_")[-1] in d.name or "arms" in d.name for d in done)]
        lines.append(f"跑批痕迹: {len(arms)} 个臂日志, 完成标记 {len(done)} 个"
                     + (f" -> ⚠️ 可能被打断: {', '.join(unfinished[:4])}" if unfinished
                        and not any("ragas_eval_demo" in l for l in ps.splitlines()) else ""))
    if fresh_boot:
        lines.append("\n若刚才有跑批在跑, 它已随重启中断, 需要重起。")
    return title, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="服务器启动/健康报告到飞书 (默认只打印)")
    ap.add_argument("--send", action="store_true", help="真的发送 (不填则只打印)")
    ap.add_argument("--to", default="")
    ap.add_argument("--chat-id", default="")
    ap.add_argument("--app", default="")
    ap.add_argument("--dedup-ttl", type=int, default=600)
    a = ap.parse_args()

    title, body = report()
    print(f"【pi · {title}】\n{body}")
    if not a.send:
        print("\n(--dry 不发送; 加 --send)")
        return 0

    w, sec = load_cred()
    app_name, app = load_app(a.app)
    rid = load_chat(a.to or a.chat_id, app)
    if not rid and not w:
        print("没有可用通道", file=sys.stderr)
        return 4
    key = f"boot_report:{int(uptime_sec() // 600)}"       # 同一次启动只发一条
    f = Path("/tmp") / f"feishu_sent_boot_{key.split(':')[1]}"
    if f.exists() and time.time() - f.stat().st_mtime < a.dedup_ttl:
        print("(跳过: 这次启动已经发过了)")
        return 0
    msg = f"【pi · {title}】\n{body}"
    print(try_send(msg, w, sec, rid, app, app_name))
    f.write_text(json.dumps({"t": time.time()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
