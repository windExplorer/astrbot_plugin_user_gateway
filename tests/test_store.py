"""存储层自检：真实建库跑一遍读写路径（不需要 AstrBot 环境）。

用法：
    uv run --no-project --with aiosqlite python tests/test_store.py

覆盖：建表 / 权限规则（含 inherit 语义）/ 额度累加与重置 / 用量日志与统计聚合 /
好友群缓存与模糊搜索 / 超期清理 / 审计日志。
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
        check(await st.get_setting("schema_version") == "1", "schema_version 已写入")
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

        print("\n[4] 额度")
        await st.upsert_quota("user", "10001", "day", 10000)
        q = await st.get_quota("user", "10001", "day")
        check(q is not None and q["limit_tokens"] == 10000 and q["used_tokens"] == 0, "新建额度")
        await st.add_used("user", "10001", 350)
        await st.add_used("user", "10001", 150)
        check((await st.get_quota("user", "10001", "day"))["used_tokens"] == 500, "累加已用量 = 500")
        # period 省略 → 该对象所有周期一起累加（main.py 每次 LLM 调用只发一条 SQL）
        await st.upsert_quota("user", "10001", "month", 100000)
        await st.add_used("user", "10001", 20)
        check((await st.get_quota("user", "10001", "day"))["used_tokens"] == 520, "省略 period → 日额度 +20")
        check((await st.get_quota("user", "10001", "month"))["used_tokens"] == 20, "省略 period → 月额度 +20")
        # 指定周期 → 只影响那一个
        await st.add_used("user", "10001", 5, "month")
        check((await st.get_quota("user", "10001", "month"))["used_tokens"] == 25, "指定 period → 只加该周期")
        check((await st.get_quota("user", "10001", "day"))["used_tokens"] == 520, "指定 period → 不影响其它周期")
        # 改额度保留已用量
        await st.upsert_quota("user", "10001", "day", 20000)
        q = await st.get_quota("user", "10001", "day")
        check(q["limit_tokens"] == 20000 and q["used_tokens"] == 520, "改额度保留已用量")
        await st.upsert_quota("user", "10001", "day", 20000, keep_used=False)
        check((await st.get_quota("user", "10001", "day"))["used_tokens"] == 0, "keep_used=False 清零")
        await st.add_used("user", "10001", 0)
        check((await st.get_quota("user", "10001", "day"))["used_tokens"] == 0, "add_used(0) 不写库")
        # 到期重置：reset_used 带上新的 reset_at
        await st.add_used("user", "10001", 777)
        n = await st.reset_used(scope_type="user", reset_at=123456)
        check(n == 2, f"reset_used 影响 2 行（day + month，实得 {n}）")
        check((await st.get_quota("user", "10001", "month"))["used_tokens"] == 0, "reset 后月额度归零")
        check((await st.get_quota("user", "10001", "day"))["reset_at"] == 123456, "reset_at 已更新")
        # 单对象额度查询与删除
        check(len(await st.list_quotas_of("user", "10001")) == 2, "list_quotas_of 返回 2 个周期")
        await st.delete_quota("user", "10001", "day")
        check(await st.get_quota("user", "10001", "day") is None, "按周期删除额度")
        check(await st.get_quota("user", "10001", "month") is not None, "只删了 day，month 仍在")
        await st.delete_quota("user", "10001")
        check(len(await st.list_quotas_of("user", "10001")) == 0, "不传 period → 删除该对象全部额度")

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

        print("\n[8] 生命周期")
        await st.close()
        check(not st.ready, "close 后 ready=False")
        await st.close()  # 幂等
        st2 = Store(db_path)
        await st2.open()
        check((await st2.list_friends())["total"] == 2, "重新打开后数据仍在")
        await st2.close()

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
