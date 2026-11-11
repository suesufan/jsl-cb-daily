# 集思录待发转债日报（云端版）

每天定时（北京时间 15:05）抓取集思录待发转债数据，生成日报并推送到个人微信（WxPusher）。
运行在 GitHub Actions 上，**不依赖本机开机**。

## 流程

1. 请求集思录 webapi：`https://www.jisilu.cn/webapi/cb/pre/`（无需浏览器）
2. 筛选 **可申购** + **同意注册** 两阶段，生成 Markdown 日报
3. 调 WxPusher API 推送到微信（Markdown 全文渲染）
4. 日报归档到 `reports/` 并提交回仓库

## 配置（GitHub Secrets）

| Secret | 说明 |
|---|---|
| `WXPUSHER_TOKEN` | WxPusher 应用的 APP_TOKEN |
| `WXPUSHER_UID` | 接收消息的 UID |

## 本地手动运行

```bash
export WXPUSHER_TOKEN="AT_xxx"
export WXPUSHER_UID="UID_xxx"
python main.py
```

## 手动触发云端运行

Actions 页面 → daily-report → Run workflow。
