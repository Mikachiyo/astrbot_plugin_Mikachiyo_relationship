# Mikachiyo关系本

将 AstrBot 人格中的人际关系表抽离为独立插件。

## 功能

- **Dashboard 可视化编辑** — 在 AstrBot WebUI 中双击编辑关系表，每个格子可独立锁定
- **LLM 工具自助维护** — AI 可自主添加/更新关系信息（受锁控制），备注限20字
- **上下文压缩时自动注入** — 在 compact / 截断触发时自动注入关系本到 system prompt 末尾（所有 system 消息之后、第一条用户消息之前），不破坏 LLM 缓存
- **补丁注入时机** — 通过猴子补丁拦截 `ContextManager.process()`。当 token 使用率超过 82% 触发压缩或轮次截断时，清理旧注入并写入最新关系本；**若当前会话尚未注入关系本，也会自动注入**
- **手动注入** — 提供 `inject_relationship` LLM 工具；同时 Dashboard 提供「手动注入」按钮与 Web API，可在独立进程（如 AI Agent、运维脚本）中触发刷新
- **独立锁定** — 每个格子可独立锁定（🔒/🔓），锁住的字段 AI 不能修改（但依然可见）

## 安装

将 `astrbot_plugin_Mikachiyo_relationship` 文件夹放入 AstrBot 的 `data/plugins/` 目录，然后在 WebUI 插件页启用。

## 使用

1. **⚠️ 重要：启用本插件后，请从 AstrBot 人格设定中删除已有的「人际关系表」整段内容**，避免重复注入
2. 在 AstrBot Dashboard 的插件页中点击「Mikachiyo关系本」进入管理界面
3. 双击单元格即可编辑，点击 🔒/🔓 切换锁定状态

## 项目结构

```
astrbot_plugin_Mikachiyo_relationship/
├── main.py              # 插件主体：SQLite + 补丁 + API 路由 + LLM tools
│   ├── 猴子补丁          # 拦截 ContextManager.process，缓存 miss 时注入关系本
│   ├── 数据库建表        # relationship + locks
│   ├── LLM tools        # update_relationship / add_user / inject_relationship
│   ├── Dashboard API    # GET/POST 路由：查询、更新、锁定、添加、手动注入
│   └── 关系本组装        # MD 表格 + 前后约束词 → system message
├── metadata.yaml        # 插件元数据
├── README.md            # 本文件
└── pages/
    └── admin/
        └── index.html   # Dashboard 管理页面
```

## 作者

Mikachiyo
