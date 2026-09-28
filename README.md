# feishu-notify

往飞书发通知/推送长文本结果的 pi skill。只发不收 (发信方向)，两条通道:

- **自建应用 + 收件人** — 群 `chat_id` / 私聊 `open_id` / 邮箱；支持多应用并存与缺权限自动回退；token 2h 缓存。
- **群自定义机器人 webhook** — 单向、免凭证体系。

配套一个**跑批守候器**（正常结束 / 超时 / 假死 三种结局都通知）和一个**开机健康报告**。

## 用法

完整说明见 [`SKILL.md`](./SKILL.md)。速览:

```bash
# 立刻发 (群)
python scripts/feishu_send.py --text "跑批完成: CR 0.5547"

# 卡片 + 长正文 (超 18000 字自动截断)
python scripts/feishu_send.py --title "判官A/B 结论" --stdin < /tmp/summary.txt

# 私聊 (按收件人自动判 receive_id_type)
python scripts/feishu_send.py --to me@corp.com --text "私聊"

# 干跑 / 诊断 / 自检 (都不碰真群)
python scripts/feishu_send.py --text "..." --dry
python scripts/feishu_send.py --status
python scripts/feishu_send.py --selftest
```

## 凭证

脚本从仓库根目录读取（全部 600 权限、已 gitignore、不在日志里明文打印）:

| 文件 | 内容 |
|---|---|
| `.feishu_apps.json` | 多应用: `{"default":"pi","apps":{"pi":{"app_id":"cli_...","app_secret":"...","chat_id":"oc_..."}}}` |
| `.feishu_app` | 一行: 默认用哪个 app |
| `.feishu_chat` | 一行: 默认收件人 (`oc_...`/`ou_...`/邮箱) |
| `.lark_key` | 两行: `app_id` / `app_secret`（旧的单应用格式，仍兼容） |
| `.feishu_webhook` / `.feishu_secret` | 群机器人 webhook URL / 签名密钥（可选） |

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/feishu_send.py` | 发信唯一实现（文本/卡片/文件、双通道、`--selftest`） |
| `scripts/notify_feishu.py` | 跑批守候器，跑完/超时/假死都会通知 |
| `scripts/boot_report.py` | 服务器重启后的开机 + 健康报告 |
| `scripts/judge_ab.py` | A/B 配对分析（Δ / 95%CI / 逐题方向） |

纯标准库，无第三方依赖 (`python>=3.8`)。
