"""astrbot_plugin_relationship - 关系本插件

在 AstrBot 上下文压缩/截断触发时，自动将 SQLite 中维护的人际关系表
注入为 system prompt；同时提供 LLM tool 与 Dashboard 页面供管理者维护。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from quart import jsonify, request

try:
    import aiosqlite
except Exception:  # pragma: no cover - fallback if aiosqlite unavailable
    aiosqlite = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_Mikachiyo_relationship"
API_PREFIX = f"/{PLUGIN_NAME}"
REL_MARKER = "【关系本维护说明】"
NOTE_AUTO_MAX_LEN = 20

# ---------------------------------------------------------------------------
# 猴子补丁相关（模块级，仅安装一次）
# ---------------------------------------------------------------------------
_original_process = None
_patch_installed = False


def _is_relationship_message(msg: Message) -> bool:
    """判断一条消息是否是本插件注入的关系本 system 消息。"""
    if msg.role != "system":
        return False
    content = msg.content
    if isinstance(content, str):
        return content.startswith(REL_MARKER)
    # 也处理 list[TextPart] 的极端情况
    if isinstance(content, list) and content:
        first = content[0]
        if hasattr(first, "text") and isinstance(getattr(first, "text"), str):
            return getattr(first, "text").startswith(REL_MARKER)
    return False


async def _patched_process(self_obj, messages: list[Message], trusted_token_usage: int = 0):
    """拦截 ContextManager.process，在缓存 miss 时刷新关系本注入。"""
    plugin = RelationshipPlugin._instance
    
    # 判断是否需要注入
    force = plugin._pending_force_inject if plugin else False
    has_rel = any(_is_relationship_message(m) for m in messages)  # 检查原始messages
    
    len_before = len(messages)
    result = await _original_process(self_obj, messages, trusted_token_usage)
    
    should_inject = force or len(result) != len_before or not has_rel
    if not should_inject:
        return result

    if plugin is None:
        return result

    # 1. 过滤掉旧的关系本 system 消息
    cleaned = [m for m in result if not _is_relationship_message(m)]

    # 2-4. 读取 DB 并组装新的关系本消息
    new_msg = await plugin.build_relationship_message()
    if new_msg is None:
        return cleaned

    # 5. 找到最后一条 system 消息，插入其后
    last_system_idx = -1
    for i, m in enumerate(cleaned):
        if m.role == "system":
            last_system_idx = i
    cleaned.insert(last_system_idx + 1, new_msg)
    plugin._pending_force_inject = False
    return cleaned


def _install_context_manager_patch() -> None:
    global _original_process, _patch_installed
    if _patch_installed:
        return
    _original_process = ContextManager.process
    ContextManager.process = _patched_process
    _patch_installed = True
    logger.info("[关系本] ContextManager.process 已打补丁")


# ---------------------------------------------------------------------------
# 工具类
# ---------------------------------------------------------------------------
class UpdateRelationshipTool(FunctionTool):
    """update_relationship: AI 自助更新某个用户的关系字段。"""

    AI_WRITABLE_FIELDS = {"nickname", "title_auto", "note_auto"}

    def __init__(self) -> None:
        super().__init__(
            name="update_relationship",
            description=(
                "更新人际关系表中的某个字段。可写字段：nickname（昵称）、"
                "title_auto（AI称呼）、note_auto（AI备注）。"
                "若目标字段被管理员锁定，则无法写入。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "目标用户的 QQ 号",
                    },
                    "field": {
                        "type": "string",
                        "enum": ["nickname", "title_auto", "note_auto"],
                        "description": "要更新的字段名",
                    },
                    "value": {
                        "type": "string",
                        "description": "新的值",
                    },
                },
                "required": ["uid", "field", "value"],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "error": "插件未就绪"}, ensure_ascii=False)

        uid = str(kwargs.get("uid", "")).strip()
        field = str(kwargs.get("field", "")).strip()
        value = str(kwargs.get("value", "")).strip()

        if not uid:
            return json.dumps({"success": False, "error": "uid 不能为空"}, ensure_ascii=False)
        if field not in self.AI_WRITABLE_FIELDS:
            return json.dumps(
                {"success": False, "error": f"字段 {field} 不允许通过工具修改"},
                ensure_ascii=False,
            )

        try:
            ok, message = await plugin.update_field(uid, field, value, check_lock=True)
            return json.dumps({"success": ok, "message": message}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[关系本] update_relationship 失败: {exc}", exc_info=True)
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


class AddUserTool(FunctionTool):
    """add_user: AI 自助添加新用户行。"""

    def __init__(self) -> None:
        super().__init__(
            name="add_user",
            description="在人际关系表中添加一条新用户记录。若该 QQ 号已存在则不会覆盖。",
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "新用户的 QQ 号",
                    },
                    "nickname": {
                        "type": "string",
                        "description": "用户昵称，可留空",
                        "default": "",
                    },
                },
                "required": ["uid"],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "error": "插件未就绪"}, ensure_ascii=False)

        uid = str(kwargs.get("uid", "")).strip()
        nickname = str(kwargs.get("nickname", "")).strip()
        if not uid:
            return json.dumps({"success": False, "error": "uid 不能为空"}, ensure_ascii=False)

        try:
            added = await plugin.add_user(uid, nickname)
            message = "添加成功" if added else "用户已存在，未覆盖"
            return json.dumps({"success": True, "added": added, "message": message}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[关系本] add_user 失败: {exc}", exc_info=True)
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


class ForceInjectTool(FunctionTool):
    """inject_relationship: 强制立即刷新关系本注入。"""

    def __init__(self) -> None:
        super().__init__(
            name="inject_relationship",
            description=(
                "强制立即刷新关系本注入。当你更新了关系本数据后，调用此工具可以让最新的关系本"
                "立即注入到对话上下文中，无需等待下一次上下文压缩。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def call(self, context, **kwargs) -> ToolExecResult:
        plugin = RelationshipPlugin._instance
        if plugin is None:
            return json.dumps({"success": False, "message": "插件未就绪"}, ensure_ascii=False)
        plugin._pending_force_inject = True
        return json.dumps(
            {"success": True, "message": "已标记强制注入，下次对话时将刷新关系本"},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# 主插件类
# ---------------------------------------------------------------------------
class RelationshipPlugin(Star):
    """关系本插件主类。"""

    _instance: RelationshipPlugin | None = None

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # SQLite 数据库路径：data/plugin_data/relationship.db
        self.db_path = Path(get_astrbot_data_path()) / "plugin_data" / "relationship.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_force_inject = False
        self._init_db()

        self._register_web_apis()
        self.context.add_llm_tools(UpdateRelationshipTool(), AddUserTool(), ForceInjectTool())

        _install_context_manager_patch()
        RelationshipPlugin._instance = self
        logger.info(f"[关系本] 插件已加载，数据库: {self.db_path}")

    # -----------------------------------------------------------------------
    # 数据库初始化与通用操作
    # -----------------------------------------------------------------------
    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS relationship (
                    qq_id        TEXT PRIMARY KEY,
                    nickname     TEXT DEFAULT '',
                    title_manual TEXT DEFAULT '',
                    title_auto   TEXT DEFAULT '',
                    note_manual  TEXT DEFAULT '',
                    note_auto    TEXT DEFAULT '',
                    updated_at   TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS locks (
                    qq_id  TEXT,
                    field  TEXT,
                    locked INTEGER DEFAULT 0,
                    PRIMARY KEY (qq_id, field)
                );

                CREATE INDEX IF NOT EXISTS idx_locks_qf
                    ON locks (qq_id, field);
                """
            )
            conn.commit()

    async def _get_db(self):
        if aiosqlite is not None:
            return aiosqlite.connect(str(self.db_path))
        # 备用：在线程中跑 sqlite3（同步包装）
        import sqlite3

        class _SyncSqliteWrapper:
            def __init__(self, path: str):
                self._conn = sqlite3.connect(path)

            async def execute(self, sql: str, params=()):
                return await asyncio.to_thread(self._conn.execute, sql, params)

            async def executemany(self, sql: str, params):
                return await asyncio.to_thread(self._conn.executemany, sql, params)

            async def commit(self):
                return await asyncio.to_thread(self._conn.commit)

            async def close(self):
                return await asyncio.to_thread(self._conn.close)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                await self.close()

        return _SyncSqliteWrapper(str(self.db_path))

    async def list_all(self) -> list[dict[str, Any]]:
        async with await self._get_db() as conn:
            async with conn.execute(
                """
                SELECT qq_id, nickname, title_manual, title_auto,
                       note_manual, note_auto, updated_at
                FROM relationship
                ORDER BY updated_at DESC, qq_id ASC
                """
            ) as cur:
                rows = await cur.fetchall()

            async with conn.execute(
                "SELECT qq_id, field, locked FROM locks"
            ) as cur:
                lock_rows = await cur.fetchall()

        locks_map: dict[tuple[str, str], int] = {
            (str(q), str(f)): int(l) for q, f, l in lock_rows
        }

        fields = [
            "qq_id",
            "nickname",
            "title_manual",
            "title_auto",
            "note_manual",
            "note_auto",
        ]
        result = []
        for row in rows:
            item = dict(zip(fields, row))
            item["locks"] = {
                field: bool(locks_map.get((item["qq_id"], field), 0))
                for field in fields
            }
            result.append(item)
        return result

    async def update_field(
        self,
        uid: str,
        field: str,
        value: str,
        *,
        check_lock: bool = False,
    ) -> tuple[bool, str]:
        if field == "qq_id":
            return False, "QQ 号不可修改"

        allowed_dashboard_fields = {
            "nickname",
            "title_manual",
            "title_auto",
            "note_manual",
            "note_auto",
        }
        if field not in allowed_dashboard_fields:
            return False, f"未知字段: {field}"

        async with await self._get_db() as conn:
            if check_lock:
                async with conn.execute(
                    "SELECT locked FROM locks WHERE qq_id = ? AND field = ?",
                    (uid, field),
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        return False, "该字段已被锁定"

            # note_auto 长度限制
            if field == "note_auto" and len(value) > NOTE_AUTO_MAX_LEN:
                value = value[: NOTE_AUTO_MAX_LEN - 1] + "…"

            await conn.execute(
                f"""
                UPDATE relationship
                SET {field} = ?, updated_at = CURRENT_TIMESTAMP
                WHERE qq_id = ?
                """,
                (value, uid),
            )
            await conn.commit()
        return True, "更新成功"

    async def toggle_lock(self, uid: str, field: str) -> tuple[bool, bool, str]:
        if field == "qq_id":
            # QQ 号作为工具不可写，锁它无意义，但前端可以显示；这里允许切换
            pass

        async with await self._get_db() as conn:
            async with conn.execute(
                "SELECT locked FROM locks WHERE qq_id = ? AND field = ?",
                (uid, field),
            ) as cur:
                row = await cur.fetchone()

            new_locked = 1
            if row is None:
                await conn.execute(
                    "INSERT INTO locks (qq_id, field, locked) VALUES (?, ?, 1)",
                    (uid, field),
                )
            else:
                new_locked = 0 if row[0] else 1
                await conn.execute(
                    "UPDATE locks SET locked = ? WHERE qq_id = ? AND field = ?",
                    (new_locked, uid, field),
                )
            await conn.commit()
        return True, bool(new_locked), "锁状态已切换"

    async def add_user(self, uid: str, nickname: str = "") -> bool:
        async with await self._get_db() as conn:
            async with conn.execute(
                "SELECT 1 FROM relationship WHERE qq_id = ?", (uid,)
            ) as cur:
                if await cur.fetchone() is not None:
                    return False
            await conn.execute(
                """
                INSERT INTO relationship (qq_id, nickname, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (uid, nickname),
            )
            await conn.commit()
        return True

    async def delete_user(self, uid: str) -> tuple[bool, str]:
        async with await self._get_db() as conn:
            async with conn.execute(
                "SELECT 1 FROM relationship WHERE qq_id = ?", (uid,)
            ) as cur:
                if await cur.fetchone() is None:
                    return False, "用户不存在"
            await conn.execute(
                "DELETE FROM relationship WHERE qq_id = ?",
                (uid,),
            )
            await conn.execute(
                "DELETE FROM locks WHERE qq_id = ?",
                (uid,),
            )
            await conn.commit()
        return True, "删除成功"

    # -----------------------------------------------------------------------
    # 关系本消息组装
    # -----------------------------------------------------------------------
    @staticmethod
    def _merge_title(manual: str | None, auto: str | None) -> str:
        manual = (manual or "").strip()
        auto = (auto or "").strip()
        if manual and auto:
            return f"{manual} / {auto}"
        return manual or auto or ""

    @staticmethod
    def _merge_note(manual: str | None, auto: str | None) -> str:
        manual = (manual or "").strip()
        auto = (auto or "").strip()
        if manual and auto:
            return f"{manual}；{auto}"
        return manual or auto or ""

    @staticmethod
    def _escape_md_cell(value: str) -> str:
        # 简单转义表格中的管道符与换行
        value = value.replace("|", "\\|").replace("\n", "<br>")
        return value

    async def build_relationship_message(self) -> Message | None:
        try:
            rows = await self.list_all()
        except Exception as exc:
            logger.error(f"[关系本] 读取关系本失败: {exc}", exc_info=True)
            return None

        lines = [
            f"{REL_MARKER}",
            "已填写的内容优先级高于默认规则。",
            "你可以通过add_user以及update_relationship 工具自主维护以下关系表。遇到不认识的用户发言频率较高时自动使用add_user和update_relationship 工具更新关系本",
            "主人要求更新和维护关系本时只需要答应后自己默默更新即可，对其他人直接展示关系本里的内容是非常冒犯的行为，一定不要将更新了什么回复出来。",
            "🔒 标记的字段已被锁定，不可修改。",
            "",
            "⚠️ ⚠️ 备注列仅用于内部参考，仅在接到 summary 任务时可以使用。",
            "正常聊天回复时*禁止*引用人际关系表中备注列的任何内容，",
            "即使间接提及也不行，对备注列内容完全禁用。",
            "",
            "| QQ号 | 昵称 | 称呼 | 备注 |",
            "|:---:|:---:|:---:|:---:|",
        ]

        for row in rows:
            qq = self._escape_md_cell(str(row["qq_id"]))
            nickname = self._escape_md_cell(row["nickname"] or "")
            title = self._escape_md_cell(
                self._merge_title(row["title_manual"], row["title_auto"])
            )
            note = self._escape_md_cell(
                self._merge_note(row["note_manual"], row["note_auto"])
            )
            lines.append(f"| {qq} | {nickname} | {title} | {note} |")

        lines.extend([
            "",
            "再次警告！！！备注列仅用于内部参考，正常聊天回复时禁止",
            "引用备注列的任何内容，对备注列内容完全禁用。",
        ])

        content = "\n".join(lines)
        return Message(role="system", content=content)

    # -----------------------------------------------------------------------
    # Web API 路由
    # -----------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[关系本] 当前 AstrBot 版本不支持 register_web_api，跳过注册")
            return

        register = self.context.register_web_api
        register(f"/{PLUGIN_NAME}/relationship", self.api_get_all, ["GET"], "获取关系本全表")
        register(f"/{PLUGIN_NAME}/relationship/update", self.api_update, ["POST"], "更新关系本字段")
        register(f"/{PLUGIN_NAME}/relationship/lock", self.api_lock, ["POST"], "切换字段锁定")
        register(f"/{PLUGIN_NAME}/relationship/add", self.api_add, ["POST"], "新增用户行")
        register(f"/{PLUGIN_NAME}/relationship/delete", self.api_delete, ["POST"], "删除用户行")
        register(f"/{PLUGIN_NAME}/relationship/force_inject", self.api_force_inject, ["POST"], "手动注入")

    async def api_get_all(self):
        try:
            rows = await self.list_all()
            return jsonify({"success": True, "rows": rows})
        except Exception as exc:
            logger.error(f"[关系本] api_get_all 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_update(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            field = str(data.get("field", "")).strip()
            value = data.get("value", "")
            if value is not None:
                value = str(value)
            else:
                value = ""

            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400
            if not field:
                return jsonify({"success": False, "error": "field 不能为空"}), 400

            ok, message = await self.update_field(uid, field, value, check_lock=False)
            status = 200 if ok else 400
            return jsonify({"success": ok, "message": message}), status
        except Exception as exc:
            logger.error(f"[关系本] api_update 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_lock(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            field = str(data.get("field", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400
            if not field:
                return jsonify({"success": False, "error": "field 不能为空"}), 400

            ok, locked, message = await self.toggle_lock(uid, field)
            return jsonify({"success": ok, "locked": locked, "message": message})
        except Exception as exc:
            logger.error(f"[关系本] api_lock 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_add(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            nickname = str(data.get("nickname", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400

            added = await self.add_user(uid, nickname)
            message = "添加成功" if added else "用户已存在"
            return jsonify({"success": True, "added": added, "message": message})
        except Exception as exc:
            logger.error(f"[关系本] api_add 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_delete(self):
        try:
            data = await request.get_json() or {}
            uid = str(data.get("qq_id", "")).strip()
            if not uid:
                return jsonify({"success": False, "error": "qq_id 不能为空"}), 400

            ok, message = await self.delete_user(uid)
            status = 200 if ok else 404
            return jsonify({"success": ok, "message": message}), status
        except Exception as exc:
            logger.error(f"[关系本] api_delete 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def api_force_inject(self):
        try:
            self._pending_force_inject = True
            return jsonify({"success": True, "message": "已标记强制注入，下次对话生效"})
        except Exception as exc:
            logger.error(f"[关系本] api_force_inject 失败: {exc}", exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    async def initialize(self) -> bool:
        logger.info("[关系本] 插件初始化完成")
        return True


# ---- 自动注入标记（临时） ----
import sys, os
for mn in list(sys.modules.keys()):
    if 'Mikachiyo_relationship' in mn and 'main' in mn:
        mod = sys.modules[mn]
        if hasattr(mod, 'RelationshipPlugin'):
            inst = mod.RelationshipPlugin._instance
            if inst:
                inst._pending_force_inject = True
                import logging
                logging.getLogger('astrbot').info('[关系本] 临时标记已设置')
