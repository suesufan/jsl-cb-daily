# -*- coding: utf-8 -*-
"""
集思录待发转债日报（云端版，无浏览器依赖）

流程：集思录 webapi 抓取 JSON → 生成 Markdown 日报 → WxPusher 推送到个人微信 → 归档到 reports/

配置（环境变量，不落代码）：
  WXPUSHER_TOKEN  WxPusher 应用的 APP_TOKEN（必填）
  WXPUSHER_UID    接收消息的 UID（必填）

安全要点：
  - 凭据仅从环境变量读取，日志输出做脱敏（仅显示前 6 位）
  - 强制 HTTPS + 证书校验 + 官方域名白名单
"""
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

API_URL = "https://www.jisilu.cn/webapi/cb/pre/"
PUSH_URL = "https://wxpusher.zjiecode.com/api/send/message"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TZ = timezone(timedelta(hours=8))  # 北京时间（GitHub Actions 运行器为 UTC）

TOKEN_ENV = "WXPUSHER_TOKEN"
UID_ENV = "WXPUSHER_UID"

BASE = Path(__file__).resolve().parent
REPORT_DIR = BASE / "reports"

FETCH_RETRIES = 3
FETCH_INTERVAL = 5  # 秒


# ============ 工具 ============
def mask(s: str) -> str:
    return f"{s[:6]}***（共{len(s)}字符）" if s else "(空)"


def fmt_num(v) -> str:
    """数值显示：None -> '-'；整数去尾零；浮点保留原精度"""
    if v is None or v == "":
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return str(v).strip()


def fmt_scale(v) -> str:
    """规模统一两位小数，如 6 -> 6.00"""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


def fmt_px(v) -> str:
    """价格统一两位小数，如 52.1 -> 52.10"""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


def classify_board(code: str) -> str:
    code = (code or "").strip()
    if re.fullmatch(r"688\d{3}", code):
        return "科创板"
    if re.fullmatch(r"60\d{4}", code):
        return "沪市主板"
    if re.fullmatch(r"(300|301)\d{3}", code):
        return "创业板"
    if re.fullmatch(r"(000|001|002|003)\d{3}", code):
        return "深市主板"
    return "—"


# ============ 抓取 ============
def fetch_rows():
    """抓取集思录 webapi，返回记录列表（自动重试）"""
    last_err = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            req = urllib.request.Request(API_URL, headers={"User-Agent": UA})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            if obj.get("code") != 200 or not isinstance(obj.get("data"), list):
                raise RuntimeError(f"接口返回异常: code={obj.get('code')} msg={obj.get('msg')}")
            rows = obj["data"]
            if not rows:
                raise RuntimeError("接口返回 0 条记录（疑似异常）")
            print(f"[FETCH] OK，共 {len(rows)} 条记录（第 {attempt} 次尝试）", flush=True)
            return rows
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[FETCH] 第 {attempt}/{FETCH_RETRIES} 次失败: {e}", flush=True)
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_INTERVAL)
    raise RuntimeError(f"抓取失败（已重试 {FETCH_RETRIES} 次）: {last_err}")


# ============ 生成日报 ============
def build_report(rows):
    """筛选 可申购 + 同意注册 两阶段，生成 Markdown"""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    ipo_list, reg_list = [], []
    for r in rows:
        progress_nm = (r.get("progress_nm") or "").strip()

        item = dict(
            stock_code=(r.get("stock_id") or "").strip(),
            stock_name=(r.get("stock_nm") or "").strip(),
            board=classify_board(r.get("stock_id")),
            bond_code=(r.get("bond_id") or "").strip(),
            bond_name=(r.get("bond_nm") or "").strip() or "-",
            scale=fmt_scale(r.get("amount")),
            rating=(r.get("rating_cd") or "-").strip(),
            conv_px=fmt_px(r.get("convert_price")),
            stock_px=fmt_px(r.get("price")),
            stock_conv=(f"{r.get('pma_rt')}%" if r.get("pma_rt") not in (None, "") else "-"),
            per_share=fmt_num(r.get("ration")),
            need_shares=fmt_num(r.get("apply10")),
        )

        if "申购" in progress_nm:
            # 申购日 / 申购代码：优先取结构化字段，缺失时从 progress_nm 解析
            apply_date = (r.get("apply_date") or "").strip()
            apply_cd = (r.get("apply_cd") or "").strip()
            if not apply_date:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", progress_nm)
                apply_date = m.group(1) if m else "-"
            if not apply_cd:
                m = re.search(r"申购代码\s*(\w+)", progress_nm)
                apply_cd = m.group(1) if m else "-"
            item["apply_date"] = apply_date
            item["apply_code"] = apply_cd
            ipo_list.append(item)
        elif progress_nm == "同意注册":
            item["pub_date"] = (r.get("progress_dt") or "-").strip()
            reg_list.append(item)

    ipo_list.sort(key=lambda x: x["apply_date"])
    reg_list.sort(key=lambda x: x["pub_date"], reverse=True)

    # 板块汇总（两阶段合计）
    summary = {}
    for it in ipo_list + reg_list:
        try:
            amt = float(it["scale"])
        except (TypeError, ValueError):
            amt = 0.0
        b = summary.setdefault(it["board"], {"n": 0, "amt": 0.0})
        b["n"] += 1
        b["amt"] += amt

    # ---- 归档版 Markdown（PC 查看用，保留完整表格）----
    md = []
    md.append(f"# 集思录 · 待发转债日报（{today}）\n")
    md.append("- 数据来源：<https://www.jisilu.cn/web/data/cb/pre>")
    md.append(f"- 抓取时间：{now.strftime('%Y-%m-%d %H:%M')}")
    md.append("- 筛选范围：**可申购** + **同意注册** 两个阶段")
    md.append(
        f"- 当日抓取总记录数：{len(rows)} 条；其中可申购 **{len(ipo_list)}** 只，"
        f"同意注册 **{len(reg_list)}** 只\n"
    )
    md.append("> 板块规则：`60xxxx` 沪市主板 · `688xxx` 科创板 · `000/001/002/003xxx` 深市主板 · `300/301xxx` 创业板\n")

    # 表一：可申购
    md.append("## 一、可申购\n")
    if not ipo_list:
        md.append("_当前无可申购的待发转债。_\n")
    else:
        md.append("| 申购日 | 正股代码 | 正股 | 板块 | 转债代码 | 转债名称 | 申购代码 | 规模(亿) | 评级 | 转股价 | 正股价 | 正股/转股价 | 配售10张所需股数(股) |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for it in ipo_list:
            md.append("| {apply_date} | {stock_code} | {stock_name} | {board} | {bond_code} | {bond_name} | {apply_code} | {scale} | {rating} | {conv_px} | {stock_px} | {stock_conv} | {need_shares} |".format(**it))
        md.append("")

    # 表二：同意注册
    md.append("## 二、同意注册\n")
    if not reg_list:
        md.append("_当前无同意注册阶段的待发转债。_\n")
    else:
        md.append("| 公告日 | 正股代码 | 正股 | 板块 | 规模(亿) | 转股价 | 正股价 | 正股/转股价 | 每股配售(元) | 配售10张所需股数(股) |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for it in reg_list:
            md.append("| {pub_date} | {stock_code} | {stock_name} | {board} | {scale} | {conv_px} | {stock_px} | {stock_conv} | {per_share} | {need_shares} |".format(**it))
        md.append("")

    # 板块汇总
    md.append("## 三、板块汇总\n")
    md.append("| 板块 | 数量 | 规模合计(亿) |")
    md.append("|---|---|---|")
    order = ["沪市主板", "深市主板", "创业板", "科创板", "—"]
    total_n, total_amt = 0, 0.0
    for b in order:
        if b in summary:
            s = summary[b]
            md.append(f"| {b} | {s['n']} | {s['amt']:.2f} |")
            total_n += s["n"]
            total_amt += s["amt"]
    md.append(f"| **合计** | **{total_n}** | **{total_amt:.2f}** |\n")

    # 风险提示
    md.append("## 风险提示（安全/合规视角）\n")
    md.append("1. 数据仅来自公开网页自动抓取，**不构成任何投资建议**。")
    md.append("2. 实际发行条款、利率、评级、赎回条款等请以交易所及上市公司正式公告为准。")
    md.append("3. **合规提醒**：科创板（需 2 年交易经验 + 50 万资产权限）、创业板（2 年交易经验 + 10 万资产权限）转债仅对应权限账户可申购，普通账户无效申购。")
    md.append("4. \"同意注册\"不等同于\"立即发行\"，仍可能因市场或公司原因延迟。")

    archive_md = "\n".join(md)

    # ---- 推送版 Markdown（手机查看用，列表式排版）----
    push = []
    push.append(f"## 📈 集思录待发转债日报 {today}")
    push.append(f"可申购 **{len(ipo_list)}** 只 · 同意注册 **{len(reg_list)}** 只\n")

    push.append("### 🟢 可申购\n")
    if not ipo_list:
        push.append("- 当前无可申购的待发转债")
    for it in ipo_list:
        push.append(
            f"- **{it['apply_date']}** `{it['apply_code']}` {it['bond_name']}"
            f"（{it['stock_name']}·{it['board']}）"
        )
        parts = [
            f"规模 {it['scale']} 亿",
            f"评级 {it['rating']}" if it["rating"] != "-" else "",
            f"转股价 {it['conv_px']}",
            f"正股价 {it['stock_px']}",
            f"正股/转股价 {it['stock_conv']}",
            f"配售10张需 {it['need_shares']} 股" if it["need_shares"] != "-" else "",
        ]
        push.append("  " + " / ".join(p for p in parts if p))

    push.append("\n### 🟡 同意注册\n")
    if not reg_list:
        push.append("- 当前无同意注册阶段的待发转债")
    for it in reg_list:
        push.append(
            f"- **{it['pub_date']}** {it['stock_name']}"
            f"（{it['board']}）规模 **{it['scale']}** 亿"
        )
        parts = [
            f"转股价 {it['conv_px']}",
            f"正股价 {it['stock_px']}",
            f"正股/转股价 {it['stock_conv']}",
            f"每股配售 {it['per_share']} 元" if it["per_share"] != "-" else "",
            f"配售10张需 {it['need_shares']} 股" if it["need_shares"] != "-" else "",
        ]
        push.append("  " + " / ".join(p for p in parts if p))

    push.append("\n> 数据：jisilu.cn · 自动抓取，不构成投资建议")

    push_md = "\n".join(push)
    card_summary = f"可申购 {len(ipo_list)} 只 · 同意注册 {len(reg_list)} 只 | 集思录待发转债日报 {today}"
    return archive_md, push_md, card_summary, today


# ============ 推送 ============
def push_wxpusher(token: str, uid: str, content: str, summary: str) -> None:
    payload = {
        "appToken": token,
        "content": content,
        "summary": summary,
        "contentType": 3,  # 3 = Markdown
        "uids": [uid],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        PUSH_URL,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    obj = json.loads(body)
    if obj.get("code") != 1000:
        raise RuntimeError(f"WxPusher 返回异常: code={obj.get('code')} msg={obj.get('msg')}")
    # 逐个接收人校验送达状态（code=1000 才是真正送达）
    failed = [d for d in (obj.get("data") or []) if d.get("code") != 1000]
    if failed:
        raise RuntimeError(f"存在未送达的接收人: {json.dumps(failed, ensure_ascii=False)}")
    print(f"[PUSH] WxPusher 推送成功: {json.dumps(obj.get('data'), ensure_ascii=False)}", flush=True)


# ============ 主流程 ============
def main() -> int:
    token = os.environ.get(TOKEN_ENV, "").strip()
    uid = os.environ.get(UID_ENV, "").strip()
    if not token or not uid:
        print(f"[ERROR] 未设置环境变量 {TOKEN_ENV} / {UID_ENV}", flush=True)
        return 1

    rows = fetch_rows()
    archive_md, push_md, card_summary, today = build_report(rows)

    # 归档（完整表格版）
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = REPORT_DIR / f"集思录-待发转债-{today}.md"
    out_file.write_text(archive_md, encoding="utf-8")
    print(f"[SAVE] 日报已归档: {out_file}", flush=True)

    # 推送（手机列表版）
    print(f"[PUSH] 目标 UID: {uid[:8]}*** / token: {mask(token)}", flush=True)
    try:
        push_wxpusher(token, uid, push_md, card_summary)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 微信推送失败: {e}", flush=True)
        return 1

    print("[DONE] 全流程完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
