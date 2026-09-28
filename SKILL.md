---
name: feishu-notify
description: >
  往飞书发通知/推送长文本结果。两条通道: ① 自建应用 + 收件人(群 chat_id / 私聊 open_id / 邮箱)
  ② 群自定义机器人 webhook。支持多应用并存 + 首选缺权限时自动回退、重试、去重、跑批守候
  (正常结束/超时/假死 三种结局都会通知)。当用户说"发飞书 / 飞书通知我 / 跑完提醒我 /
  把结果推给我 / 推送到飞书", 或需要把跑批结论、报告摘要、告警、长日志在任务结束时送达时用。
  只发不收 —— 在飞书里 @机器人 给 agent 派活属于双向, 需要自建应用的**事件订阅**, 而一个 app
  的事件订阅只能有一个消费者(现在是 OpenClaw), 本 skill 不覆盖。
---

# 飞书通知 (feishu-notify)

只做一件事: **把文本发到飞书**。长正文自动截断, 卡片/文本自动选, 失败给可读原因。

## 两条通道

| 通道 | 凭证 | 特点 |
|---|---|---|
| **自建应用 + 收件人** | `.feishu_apps.json`(多应用) 或 `.lark_key`(旧的单应用两行); `.feishu_chat` 一行收件人 | 能指定群/私聊/邮箱; token 2h 缓存; 推荐 |
| 群自定义机器人 webhook | `.feishu_webhook` 一行 URL; 可选 `.feishu_secret` | 单向、免凭证体系; 群里要有"自定义机器人"权限 |

有收件人就走自建应用；否则才走 webhook。`deliver()` 是唯一发信实现, 两个脚本共用。

### 多应用 + 自动回退 (`.feishu_apps.json`)

```json
{"default": "pi",
 "apps": {"pi":       {"app_id": "cli_...", "app_secret": "...", "chat_id": "oc_..."},
          "openclaw": {"app_id": "cli_...", "app_secret": "...", "chat_id": "oc_..."}}}
```

**首选 app 报 `99991672`(缺发消息权限) 时自动换另一个已配 app 发出去**, 并在回执里注明
「首选(x)无权限, 已回退」。所以给某个 app 申请权限期间通知不会断, 权限批下来后自动接管。

凭证解析优先级: `--app-id/--app-secret` > `--app`/`$FEISHU_APP` > `.feishu_app` > JSON 的 `default` > `.lark_key`。
收件人: `--to`/`--chat-id` > `$FEISHU_CHAT_ID` > `.feishu_chat` > app 里的 `chat_id`。

## 用法

```bash
S=.venv/bin/python
# ① 立刻发 (群)
$S scripts/feishu_send.py --text "跑批完成: CR 0.5547"
# ② 卡片 + 长正文
$S scripts/feishu_send.py --title "判官A/B 结论" --stdin < /tmp/summary.txt
# ③ 私聊 (按收件人长相自动判 receive_id_type: oc_群 / ou_用户 / 邮箱 / user_id)
$S scripts/feishu_send.py --to me@corp.com --text "私聊"
# ④ 干跑 / 诊断 / 自检 (都不碰真群)
$S scripts/feishu_send.py --text "..." --dry
$S scripts/feishu_send.py --status          # 通道 + 各 app(掩码) + 验 token
$S scripts/feishu_send.py --selftest        # 内建假飞书, 11 个用例
# ⑤ 去重 (同 key 600s 内只发一次)
$S scripts/feishu_send.py --text "..." --dedup-key daily-report
```

### 跑批守候器

```bash
nohup $S scripts/notify_feishu.py \
  --wait-file /tmp/ab_arms_DONE \
  --log-a /tmp/arm_a.log --log-b /tmp/arm_b.log --label-a A --label-b B \
  --pid-a <驱动 pid> --idle-timeout 1800 --grace 240 \
  < /dev/null > /tmp/notify.log 2>&1 &
ps -ef | grep notify_feishu
```

三种结局**都会发消息**（不会静默失效）:

| 结局 | 触发 | 内容 |
|---|---|---|
| 正常 | `--wait-file` 出现 | 跑 `judge_ab.py` 配对分析 → Δ / 95%CI / 逐题方向 / 裁判名 / 各臂 cases-scored-failed |
| 超时 | `--wait-timeout` (默认 6h) | "未完成" + 两臂进度 |
| **假死** | 指定 pid 全不在 或 日志 `--idle-timeout` 秒没更新 | "异常" + 进度 —— 跑批被 OOM 杀掉时不用干等 6 小时 |

`--grace`(默认 180s)是防误判: 跑批刚结束那一瞬间进程/日志本来就会停, 假死信号要持续
超过 grace 才报。报告路径优先 `--report-a/b`, 其次从日志抓 `report=`, 最后按修改时间取最新
(这条会附警告, 因为 A/B 对应关系不保证)。

## 开机/健康报告 (boot_report.py)

服务器夜里重启 → 挂着的跑批和守候器全没了, 而你不知道。这条补上这个盲区。

```bash
$S scripts/boot_report.py          # 默认只打印 (--dry 语义)
$S scripts/boot_report.py --send   # 真发
```

内容: 启动时间/已运行多久 + 关键容器(rag-service / milvus-*) + `readyz` 状态 +
常驻进程(GPU租约守护 / 跑批守候器 / 跑批)在不在 + 有没有**跑批被打断**的痕迹。
自动区分"刚开机(总“服务器重启”)"与"手动跑(总“服务器状态”)"。同一次启动只发一条。

已装在服务器用户 crontab (与 `gpu_lease.sh watch` 并存, 别互相覆盖):

```cron
@reboot /bin/bash /window_share/rag/deploy/vs-server/gpu_lease.sh watch >/dev/null 2>>/window_share/rag-gpu/watch.log
@reboot sleep 90; cd /window_share/rag && ./.venv/bin/python scripts/boot_report.py --send >> /tmp/boot_report.log 2>&1
```

改 crontab 要**幂等 + 先备份**: `crontab -l > /tmp/ct.bak`, 追加后 `crontab /tmp/ct.bak`,
完事 `crontab -l | grep -c gpu_lease` 确认原来那行还在。

## 铁律

1. **先 `--dry` 再真发**；长正文尤其。
2. **正文 ≤18000 字**, 超了自动截断并标注。
3. **别打印 webhook / app_secret / token**；诊断只报"已配"和掩码。
4. **后台起守候必须 `nohup … < /dev/null > log 2>&1 &`**，之后 `ps -ef` 确认。
   少了 `< /dev/null`，SSH 客户端等不到 EOF 会假超时（远端其实在跑）。
5. **飞书失败也返 HTTP 200** —— 必须解 body 的 `code`（脚本已处理）。
6. **`.lark_key` 本机与服务器可能是两个不同的 app**（本地那个常没发消息权限）→ 发飞书在服务器上跑。

## 报错对照

| 现象 | 原因 / 处理 |
|---|---|
| `99991672 Access denied. scopes required: [im:message:send...]` | 该 app 没开「以应用身份发消息」。去 `https://open.feishu.cn/app/<app_id>/auth?q=im:message:send_as_bot` 开通 → 应用功能里启用**机器人** → **创建版本并发布** → 把机器人**加进目标群**(否则报 bot not in chat) |
| `19001 incoming webhook access token invalid` | webhook 是占位符/抄错，不是真 URL |
| `19021 sign match fail` | 机器人开了签名校验，需 `.feishu_secret` |
| `230002 bot is not in the chat` | 应用发了权限但机器人还没被拉进群 |
| 全部 app 都 `99991672` | 没有可用应用 → 先在飞书把权限/发布/入群走完 |

## 自检

`scripts/feishu_send.py --selftest` 起一个假飞书开放平台(token + 发消息两个端点)并断言:
报文结构、卡片换行、超长截断、收件人类型判定、占位符预检、body code 校验、token 缓存、
**缺权限自动回退**。不碰真群, 幂等。
