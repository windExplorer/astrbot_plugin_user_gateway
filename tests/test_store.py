"""存储层自检：真实建库跑一遍读写路径（不需要 AstrBot 环境）。

用法：
    uv run --no-project --with aiosqlite python tests/test_store.py

覆盖：建表 / 权限规则（含 inherit 语义）/ 限额与用量分离 / 等级与归级 /
bot 最后消息 / 用量日志与统计聚合 / 好友群缓存与模糊搜索 / 超期清理 / 审计 /
v1 → v2 结构迁移。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import Store  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _failures.append(label)


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "user_gateway.db")
        st = Store(db_path)
        await st.open()

        print("\n[1] 建库")
        check(st.ready, "Store.ready")
        check(Path(db_path).exists(), "数据库文件已创建")

        print("\n[2] settings")
        check(await st.get_setting("schema_version") == "5", "schema_version 已写入 5")
        check(await st.get_setting("nope", "d") == "d", "缺省值回退")
        await st.set_setting("sync_last_at", "123")
        check(await st.get_setting("sync_last_at") == "123", "写入后可读")

        print("\n[3] 权限规则")
        check(await st.get_effect("user", "10001") is None, "未配置 → None（继承）")
        await st.set_policy("user", "10001", "deny")
        check(await st.get_effect("user", "10001") == "deny", "写入 deny")
        await st.set_policy("user", "10001", "allow")
        check(await st.get_effect("user", "10001") == "allow", "同键覆盖为 allow")
        await st.set_policy("group", "88888", "deny")
        check(await st.effect_map("user") == {"10001": "allow"}, "effect_map(user)")
        check(await st.effect_map("group") == {"88888": "deny"}, "effect_map(group)")
        await st.set_policy("user", "10001", "inherit")
        check(await st.get_effect("user", "10001") is None, "inherit 等价于删除")

        print("\n[4] 限额与用量（v2 起两者分离）")
        await st.upsert_quota("user", "10001", "day", 10000)
        q = await st.get_quota("user", "10001", "day")
        check(q is not None and q["limit_tokens"] == 10000, "新建限额")
        check("used_tokens" not in q, "限额行不再自带用量（用量在 usage_counter）")
        await st.add_used("user", "10001", 350, reset_at_map={"day": 999999})
        await st.add_used("user", "10001", 150)
        u = await st.get_usage("user", "10001")
        check(u["day"]["used_tokens"] == 500, f"用量累加 = 500（实得 {u['day']['used_tokens']}）")
        check(u["month"]["used_tokens"] == 500 and u["total"]["used_tokens"] == 500, "日/月/累计三个周期一起累加")
        check(u["day"]["reset_at"] == 999999, "首次写入时记录 reset_at")
        check(u["month"]["reset_at"] is None, "未指定 reset_at 的周期留空")
        # reset_at 不该被后续累加覆盖（否则重置时间会被一直往后推）
        await st.add_used("user", "10001", 10, reset_at_map={"day": 111})
        check((await st.get_usage("user", "10001"))["day"]["reset_at"] == 999999, "后续累加不覆盖已有 reset_at")
        # 改限额不影响用量
        await st.upsert_quota("user", "10001", "day", 20000)
        check((await st.get_usage("user", "10001"))["day"]["used_tokens"] == 510, "改额度不清零用量")
        check((await st.get_quota("user", "10001", "day"))["limit_tokens"] == 20000, "上限已更新")
        # 未配额度的对象也必须记账（等级/全局模板要用它自己的用量判定）
        await st.add_used("user", "20002", 77)
        check((await st.get_usage("user", "20002"))["day"]["used_tokens"] == 77, "未配额度也记账")
        check(await st.add_used("user", "20002", 0) == 0, "add_used(0) 不写库")
        um = await st.usage_map("user")
        check(set(um) == {"10001", "20002"}, f"usage_map 覆盖全部对象（实得 {sorted(um)}）")
        # 清零
        await st.reset_used(scope_type="user", scope_id="10001", period="day", reset_at=123456)
        u = await st.get_usage("user", "10001")
        check(u["day"]["used_tokens"] == 0 and u["day"]["reset_at"] == 123456, "按周期清零并写回新的 reset_at")
        check(u["month"]["used_tokens"] == 510, "只清了日周期")
        due = await st.counters_due(123456)
        check(len(due) == 1 and due[0]["scope_id"] == "10001" and due[0]["period"] == "day",
              f"counters_due 只列出到期的用量行（实得 {len(due)} 条）")
        check(await st.counters_due(123455) == [], "未到点的不出现在 counters_due")
        check(await st.counters_due(10**12) == due, "远期时间点结果一致（其它行 reset_at 为空）")
        # 删除限额
        await st.upsert_quota("user", "10001", "month", 100000)
        check(len(await st.list_quotas_of("user", "10001")) == 2, "list_quotas_of 返回 2 个周期")
        await st.delete_quota("user", "10001", "day")
        check(await st.get_quota("user", "10001", "day") is None, "按周期删除限额")
        check(await st.get_quota("user", "10001", "month") is not None, "只删了 day，month 仍在")
        await st.delete_quota("user", "10001")
        check(len(await st.list_quotas_of("user", "10001")) == 0, "不传 period → 删除该对象全部限额")
        check((await st.get_usage("user", "10001"))["month"]["used_tokens"] == 510, "删限额不影响用量")

        print("\n[5] 用量日志与统计")
        now = int(time.time())
        for i in range(3):
            await st.log_usage(
                ts=now - i * 60,
                scope_type="user",
                scope_id="10001",
                sender_id="10001",
                group_id="",
                provider_id="p1",
                model="gpt-4o",
                tok_in_other=100,
                tok_in_cached=50,
                tok_out=30,
                status="ok",
                latency_ms=1200,
            )
        await st.log_usage(
            ts=now,
            scope_type="group",
            scope_id="88888",
            sender_id="10001",
            group_id="88888",
            status="denied",
            deny_reason="quota",
            kind="llm",
        )
        await st.log_usage(ts=now, scope_type="user", scope_id="10002", sender_id="10002", status="ok", tok_out=1, estimated=1)
        res = await st.query_usage(limit=10)
        check(res["total"] == 5, f"明细总数 = {res['total']}（期望 5）")
        check(len(res["rows"]) == 5, "返回 5 行")
        res2 = await st.query_usage(status="denied")
        check(res2["total"] == 1 and res2["rows"][0]["deny_reason"] == "quota", "按 status 过滤")

        s = await st.summary(now - 3600, now + 60)
        t = s["totals"]
        check(t["calls"] == 5, f"calls = {t['calls']}（期望 5）")
        check(t["denied"] == 1, f"denied = {t['denied']}（期望 1）")
        check(t["tok_in_other"] == 300 and t["tok_in_cached"] == 150 and t["tok_out"] == 91,
              f"token 分项 = {t['tok_in_other']}/{t['tok_in_cached']}/{t['tok_out']}（期望 300/150/91）")
        check(t["tok_total"] == 541, f"tok_total = {t['tok_total']}（期望 541）")
        check(t["users"] == 2, f"活跃用户 = {t['users']}（期望 2）")
        check(t["groups"] == 1, f"活跃群 = {t['groups']}（期望 1）")
        check(len(s["trend"]) == 1, f"趋势按天分桶 = {len(s['trend'])} 天")
        check(s["trend"][0]["tokens"] == 541, "趋势当日 token")
        check(any(r["reason"] == "quota" for r in s["deny_reasons"]), "拒绝原因含 quota")
        check(any(r["model"] == "gpt-4o" for r in s["by_model"]), "模型占比含 gpt-4o")

        # 按维度聚合（控制台列表页的「今日用量」就靠它）
        sums = await st.usage_sums("sender_id", now - 3600, now + 60)
        check(sums.get("10001") == 540, f"sender_id 聚合 10001 = 540（实得 {sums.get('10001')}）")
        check(sums.get("10002") == 1, "sender_id 聚合 10002 = 1")
        gsums = await st.usage_sums("group_id", now - 3600, now + 60)
        check(gsums.get("88888") == 0, "group_id 聚合 88888 = 0（只记了拒绝事件）")
        try:
            await st.usage_sums("tok_out; DROP TABLE usage_log", now - 3600, now + 60)
            check(False, "非白名单列应被拒绝")
        except ValueError:
            check(True, "非白名单列被拒绝（防注入）")

        print("\n[5.1] 指令统计与 CSV 导出（M6）")
        for i, (cmd, state) in enumerate(
            [
                ("帮助", "ok"),
                ("帮助", "ok"),
                ("签到", "ok"),
                ("帮助", "denied"),
                ("签到", "denied"),
                ("签到", "denied"),
                ("签到", "denied"),
            ],
        ):
            await st.log_usage(
                ts=now - i * 60,
                scope_type="user",
                scope_id="10001",
                sender_id="10001",
                kind="command",
                command_name=cmd,
                status=state,
                deny_reason="command" if state == "denied" else "",
            )
        top_ok = await st.top_commands(now - 3600, now + 60, status="ok")
        check(top_ok[0]["command"] == "帮助" and int(top_ok[0]["cnt"]) == 2, f"最常触发排行 = {top_ok}")
        check(top_ok[1]["command"] == "签到" and int(top_ok[1]["cnt"]) == 1, "排行按次数降序")
        top_denied = await st.top_commands(now - 3600, now + 60, status="denied")
        check(
            top_denied[0]["command"] == "签到" and int(top_denied[0]["cnt"]) == 3,
            f"最常被拦排行 = {top_denied}",
        )
        # 关键：指令流水不能污染 LLM 口径
        s2 = await st.summary(now - 3600, now + 60)
        check(s2["totals"]["calls"] == 5, f"指令流水不影响 calls（实得 {s2['totals']['calls']}）")
        check(s2["totals"]["tok_total"] == 541, "指令流水不带 token，总量不变")
        heat = await st.hour_heatmap(now - 3600, now + 60, kind="command")
        check(sum(int(r["cnt"]) for r in heat) == 7, "热力图按 (星期, 小时) 分桶，覆盖 7 条指令事件")
        csv_text = await st.export_usage_csv(from_ts=now - 3600, to_ts=now + 60)
        check(csv_text.startswith("\ufeff"), "CSV 带 UTF-8 BOM（Excel 不乱码）")
        # 注意 BOM 会留在首行开头，比较前先脱掉（不脱的话 startswith 必然为假）
        csv_lines = [ln for ln in csv_text.splitlines() if ln]
        head = csv_lines[0].lstrip("\ufeff")
        check(head.startswith("时间,类型"), f"CSV 表头正确：{head[:16]}")
        check(len(csv_lines) == 1 + 12, f"CSV 行数 = 表头 + 12 条（实得 {len(csv_lines)}）")
        check("指令" in csv_text and "帮助" in csv_text, "CSV 含指令类型与指令名")
        check((await st.export_usage_csv(kind="command")) .count("\n") == 8, "导出支持按类型过滤（7 条指令 + 表头）")

        # 单对象统计（详情抽屉用）
        su = await st.subject_stats("user", "10001", now - 3600, now + 60)
        check(su["totals"]["calls"] == 4, f"用户统计 calls = 4（实得 {su['totals']['calls']}）")
        check(su["totals"]["tok_total"] == 540, "用户统计 tok_total = 540")
        check(len(su["series"]) == 1, "用户统计曲线按天 1 个点")
        sg = await st.subject_stats("group", "88888", now - 3600, now + 60)
        check(sg["totals"]["events"] == 1 and sg["totals"]["denied"] == 1, "群统计：1 个事件且为拒绝")

        print("\n[6] 好友 / 群缓存")
        await st.upsert_friends("aiocqhttp", [
            {"user_id": "10001", "nickname": "小明", "remark": "同事"},
            {"user_id": "10002", "nickname": "小红", "remark": ""},
            {"nickname": "没有号"},  # 应被过滤
        ])
        fr = await st.list_friends()
        check(fr["total"] == 2, f"好友数 = {fr['total']}（期望 2，无 user_id 的条目被丢弃）")
        fr2 = await st.list_friends(keyword="同事")
        check(fr2["total"] == 1 and fr2["rows"][0]["uin"] == "10001", "按备注搜索")
        fr3 = await st.list_friends(keyword="小红")
        check(fr3["total"] == 1, "按昵称搜索")
        await st.upsert_friends("aiocqhttp", [{"user_id": "10001", "nickname": "小明改名", "remark": "同事"}])
        fr4 = await st.list_friends(keyword="小明改名")
        check(fr4["total"] == 1, "重复同步为覆盖更新")
        check((await st.list_friends())["total"] == 2, "覆盖后总数不变")

        await st.upsert_groups("aiocqhttp", [
            {"group_id": "88888", "group_name": "测试群", "member_count": 30, "max_member_count": 200, "owner": "10001"},
            {"group_id": "99999", "group_name": "摸鱼群", "member_count": 5},
        ])
        gr = await st.list_groups()
        check(gr["total"] == 2, "群数 = 2")
        check(gr["rows"][0]["group_id"] == "88888", "按人数倒序，88888 在前")
        check((await st.list_groups(keyword="摸鱼"))["total"] == 1, "按群名搜索")

        # 单个对象查询（详情抽屉的基础信息）
        f1 = await st.get_friend("10001")
        check(f1 is not None and f1["nickname"] == "小明改名" and f1["remark"] == "同事", "get_friend 取到改名后的记录")
        check(await st.get_friend("77777") is None, "不存在的 QQ → None")
        g1 = await st.get_group("88888")
        check(g1 is not None and g1["name"] == "测试群" and g1["owner"] == "10001", "get_group 字段完整")

        print("\n[7] 超期清理与审计")
        await st.log_usage(ts=now - 400 * 86400, scope_type="user", scope_id="10001", status="ok")
        before = (await st.query_usage(limit=1))["total"]
        await st.purge_old_usage(180)
        after = (await st.query_usage(limit=1))["total"]
        check(after == before - 1, f"清理 180 天前明细: {before} → {after}")
        check(await st.purge_old_usage(0) == 0, "retention_days=0 表示不清理")

        await st.log_audit("console", "set_policy", "{}")
        au = await st.list_audit()
        check(au["total"] == 1 and au["rows"][0]["action"] == "set_policy", "审计日志")

        print("\n[8] 等级与归级")
        lv_normal = await st.upsert_level("user", "普通", description="默认档", effect="inherit", sort_order=1)
        lv_vip = await st.upsert_level("user", "VIP", effect="allow", sort_order=2)
        lv_group = await st.upsert_level("group", "主群", effect="deny")
        check(len(await st.list_levels()) == 3, "等级数 = 3")
        check(len(await st.list_levels("user")) == 2, "按 kind 过滤")
        check(await st.upsert_level("user", "VIP", effect="allow") == lv_vip, "同名等级重复 upsert 返回同一 id")
        lv = await st.get_level(lv_vip)
        check(lv["kind"] == "user" and lv["effect"] == "allow", "get_level 字段正确")
        check(await st.get_level(99999) is None, "不存在的等级 → None")

        await st.upsert_quota("level", str(lv_vip), "day", 500000)
        check(len(await st.list_quotas_of("level", str(lv_vip))) == 1, "等级额度模板可写")
        levels_lim = await st.list_quotas("level")
        check(len(levels_lim) == 1 and levels_lim[0]["scope_id"] == str(lv_vip), "list_quotas('level') 能取到模板")
        glob = await st.list_quotas("global")
        await st.upsert_quota("global", "*", "day", 50000)
        check(len(await st.list_quotas("global")) == 1 and glob == [], "全局模板独立成一层")

        await st.set_subject_level("user", "10001", lv_vip)
        check(await st.get_subject_level("user", "10001") == lv_vip, "归级写入")
        check(await st.subject_level_map("user") == {"10001": lv_vip}, "subject_level_map")
        check((await st.level_counts()).get(lv_vip) == 1, "level_counts 统计成员数")
        await st.set_subject_level("user", "10001", lv_vip)  # 幂等
        check((await st.level_counts()).get(lv_vip) == 1, "重复归级不重复计数")
        await st.set_subject_level("user", "10001", None)
        check(await st.get_subject_level("user", "10001") is None, "取消归级")

        await st.upsert_level("user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9)
        lv = await st.get_level(lv_vip)
        check(lv["name"] == "VIP改名" and lv["effect"] == "deny" and lv["sort_order"] == 9, "按 id 更新等级")
        check(len(await st.list_levels()) == 3, "更新不会多出等级")

        # 等级模型路由（主提供商 / 备用提供商；模型以提供商为单位，不单独存模型名）
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
        )
        lv = await st.get_level(lv_vip)
        check(lv["provider_id"] == "prov-a" and lv["fallback_provider_id"] == "prov-b",
              "等级模型路由字段可写可读")
        check("model" not in lv, "等级表已无 model 列（模型以提供商为单位）")
        # 等级默认「指令」权限（与 LLM 权限分开，v5）
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b", command_effect="deny",
        )
        lv = await st.get_level(lv_vip)
        check(lv["effect"] == "deny" and lv["command_effect"] == "deny",
              "等级的两套默认权限（LLM / 指令）互不影响、各自可读写")
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, effect="allow", sort_order=9)
        lv = await st.get_level(lv_vip)
        check(lv["command_effect"] == "inherit", "不传指令权限 → 回到 inherit（避免旧值阴魂不散）")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
        )
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9)
        lv = await st.get_level(lv_vip)
        check(lv["provider_id"] == "" and lv["fallback_provider_id"] == "",
              "不传模型字段 → 清空（避免旧值阴魂不散）")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
        )

        await st.set_subject_level("group", "88888", lv_group)
        await st.delete_level(lv_group)
        check(await st.get_level(lv_group) is None, "删除等级")
        check(await st.get_subject_level("group", "88888") is None, "删除等级后归级记录一并清除")
        check((await st.level_counts()).get(lv_group) is None, "删除等级后成员统计同步清空")

        print("\n[9] bot 最后消息")
        await st.set_bot_message("user", "10001", "llm", ts=1000, preview="你好呀", platform_id="p1")
        await st.set_bot_message("group", "88888", "command", ts=2000, command="help", preview="/help")
        b1 = await st.get_bot_message("user", "10001")
        check(b1["kind"] == "llm" and b1["preview"] == "你好呀" and b1["platform_id"] == "p1", "写入后读取")
        await st.set_bot_message("user", "10001", "normal", ts=3000, preview="覆盖")
        b1 = await st.get_bot_message("user", "10001")
        check(b1["ts"] == 3000 and b1["kind"] == "normal" and b1["preview"] == "覆盖",
              "同一会话覆盖式更新（只留最新一条）")
        m = await st.bot_message_map("group")
        check(m["88888"]["command"] == "help", "bot_message_map 按作用域取")
        check(len(await st.bot_message_map("user")) == 1, "bot_message_map(user) 只有 1 个会话")
        sm = await st.bot_message_summary(0, 4000)
        check(sm["sessions"] == 2, f"会话数 = {sm['sessions']}（期望 2）")
        check(any(k["kind"] == "normal" and k["cnt"] == 1 for k in sm["kinds"]), "类型分布含 normal")
        check(sm["latest_ts"] == 3000, "最新时间 = 3000")
        await st.set_bot_message("user", "", "llm")  # 空 scope_id 应被忽略
        check((await st.bot_message_summary(0, 10**12))["sessions"] == 2, "空 id 不产生记录")

        print("\n[10] 生命周期")
        await st.close()
        check(not st.ready, "close 后 ready=False")
        await st.close()  # 幂等
        st2 = Store(db_path)
        await st2.open()
        check((await st2.list_friends())["total"] == 2, "重新打开后数据仍在")
        check((await st2.get_usage("user", "10001"))["month"]["used_tokens"] == 510, "重新打开后用量计数仍在")
        await st2.close()

    print("\n[11] v1 → v2 迁移")
    with tempfile.TemporaryDirectory() as tmp2:
        v1_path = str(Path(tmp2) / "v1.db")
        import aiosqlite

        raw = await aiosqlite.connect(v1_path)
        await raw.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE llm_quota (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type   TEXT    NOT NULL,
                scope_id     TEXT    NOT NULL,
                period       TEXT    NOT NULL,
                limit_tokens INTEGER NOT NULL,
                used_tokens  INTEGER NOT NULL DEFAULT 0,
                mode         TEXT    NOT NULL DEFAULT 'enforce',
                reset_at     INTEGER,
                updated_at   INTEGER NOT NULL,
                UNIQUE (scope_type, scope_id, period)
            );
            CREATE INDEX idx_quota_scope ON llm_quota(scope_type, scope_id);
            """,
        )
        await raw.execute("INSERT INTO settings(key, value, updated_at) VALUES('schema_version', '1', 0)")
        await raw.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, used_tokens, mode, reset_at, updated_at) "
            "VALUES('user', '10001', 'day', 1000, 600, 'enforce', 555, 0)"
        )
        await raw.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, used_tokens, mode, updated_at) "
            "VALUES('user', '10001', 'month', 50000, 0, 'observe', 0)"
        )
        await raw.commit()
        await raw.close()

        st3 = Store(v1_path)
        await st3.open()
        check(await st3.get_setting("schema_version") == "5", "版本号直接升到最新（v1 → v5 连续迁移）")
        q = await st3.get_quota("user", "10001", "day")
        check(q is not None and q["limit_tokens"] == 1000 and "used_tokens" not in q,
              "限额保留、用量的列已移除")
        check((await st3.get_usage("user", "10001"))["day"]["used_tokens"] == 600, "v1 已用量迁入 usage_counter")
        check((await st3.get_usage("user", "10001"))["day"]["reset_at"] == 555, "reset_at 一并迁移")
        check((await st3.get_usage("user", "10001")).get("month") is None, "用量为 0 的行不迁移")
        check(await st3.get_quota("user", "10001", "month") is not None, "另一个周期限额也在")
        check(await st3.get_quota("user", "10001", "month") is not None
              and (await st3.get_quota("user", "10001", "month"))["mode"] == "observe", "模式保留")
        async with st3._conn().execute("PRAGMA index_list(llm_quota)") as cur:
            idx = {str(r["name"]) for r in await cur.fetchall()}
        check("idx_quota_scope" in idx, f"重建后索引仍存在（实得 {sorted(idx)}）")
        # 二次打开不应重复迁移
        await st3.close()
        st4 = Store(v1_path)
        await st4.open()
        check((await st4.get_usage("user", "10001"))["day"]["used_tokens"] == 600, "重开不会重复累加迁移用量")
        await st4.close()

    print("\n[12] v2 → v4 迁移（等级新增模型路由两列、并去掉 v3 临时的 model 列）")
    with tempfile.TemporaryDirectory() as tmp3:
        v2_path = str(Path(tmp3) / "v2.db")
        raw2 = await aiosqlite.connect(v2_path)
        await raw2.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE quota_level (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                effect      TEXT    NOT NULL DEFAULT 'inherit',
                sort_order  INTEGER NOT NULL DEFAULT 0,
                updated_at  INTEGER NOT NULL,
                UNIQUE (kind, name)
            );
            """,
        )
        await raw2.execute("INSERT INTO settings(key, value, updated_at) VALUES('schema_version', '2', 0)")
        await raw2.execute("INSERT INTO quota_level(kind, name, updated_at) VALUES('user', '老等级', 0)")
        await raw2.commit()
        await raw2.close()

        st5 = Store(v2_path)
        await st5.open()
        check(await st5.get_setting("schema_version") == "5", "版本号升到 5（v2 → v3 → v4 → v5 连续迁移）")
        lv = await st5.get_level(1)
        check(lv is not None and lv["name"] == "老等级", "v2 的等级数据保留")
        check(
            lv.get("provider_id") == "" and lv.get("fallback_provider_id") == "" and "model" not in lv,
            "模型路由两列默认为空，且 v3 临时加的 model 列已被 v4 移除",
        )
        check(lv.get("command_effect") == "inherit", "v5 新增的 level.command_effect 默认为 inherit")
        await st5.upsert_level("user", "老等级", level_id=1, provider_id="p1", fallback_provider_id="p2")
        lv = await st5.get_level(1)
        check(
            lv["provider_id"] == "p1" and lv["fallback_provider_id"] == "p2",
            "迁移后两列可正常读写",
        )
        await st5.close()
        st6 = Store(v2_path)
        await st6.open()
        check((await st6.get_level(1))["provider_id"] == "p1", "重开不会重复迁移、也不丢数据")
        await st6.close()

    print("\n[13] 指令规则（policy 的指令 feature 维度）")
    with tempfile.TemporaryDirectory() as tmp4:
        st7 = Store(str(Path(tmp4) / "cmd.db"))
        await st7.open()
        await st7.set_policy("global", "*", "deny", feature="command:help")
        await st7.set_policy("user", "10001", "allow", feature="command:help")
        await st7.set_policy("group", "88888", "deny", feature="command:draw")
        cp = await st7.command_policies()
        check(cp["help"]["global"]["*"] == "deny", "全局指令规则可读写")
        check(cp["help"]["user"]["10001"] == "allow", "好友指令规则可读写")
        check(cp["draw"]["group"]["88888"] == "deny", "群指令规则可读写")
        check(set(cp) == {"help", "draw"}, f"command_policies 只含指令 feature（实得 {sorted(cp)}）")
        check(await st7.effect_map("global", feature="command:help") == {"*": "deny"}, "effect_map 支持任意 feature")
        check(len(await st7.list_policies(feature="command:help")) == 2, "按 feature 过滤")
        check(len(await st7.list_policies(feature="llm")) == 0, "指令规则不污染 llm feature")
        check(len(await st7.list_policies()) == 3, "不传 feature → 全部规则")
        await st7.set_policy("user", "10001", "inherit", feature="command:help")
        check(len(await st7.list_policies(feature="command:help")) == 1, "inherit 删除该条指令规则")
        check(await st7.delete_stale_command_policies(["help", "draw"]) == 0, "指令都还在 → 不清理")
        await st7.set_policy("user", "20002", "deny", feature="command:old")
        n = await st7.delete_stale_command_policies(["help", "draw"])
        check(n == 1, f"指令被卸载后残留的规则会被清理（实得 {n} 条）")
        check("old" not in await st7.command_policies(), "清理后该指令规则消失")
        check(len(await st7.list_policies(feature="command:help")) == 1, "不相关的指令规则不受影响")
        await st7.close()

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  - " + f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
