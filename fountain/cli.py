"""命令行：初始化、演示样例、进程内调度 + Web 服务。

本地启动：
    python -m fountain.cli serve              # 建库 + 调度器 + Web（http://127.0.0.1:8000）
    python -m fountain.cli demo-sample        # 一条命令生成超限/天气停运/复开退回样例
    python -m fountain.cli reset              # 重置演示数据
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime

from . import adapters, config, scheduler, services
from .db import connect, init_db
from .seed import seed


def cmd_init(args) -> None:
    config.ensure_dirs()
    conn = connect(args.db)
    init_db(conn)
    if args.seed:
        seed(conn)
    print(f"数据库就绪：{config.DB_PATH if not args.db else args.db}")


def cmd_reset(args) -> None:
    from .seed import reset_and_seed
    conn, _ = reset_and_seed(args.db)
    print("已重置并写入演示数据。")
    conn.close()


def cmd_demo_sample(args) -> None:
    """生成三类样例：水质超限拦截、天气停运、复开退回。"""
    config.ensure_dirs()
    conn = connect(args.db)
    init_db(conn)
    pool_ids = seed(conn)  # 库已有时不重建
    name_to_id = {r["name"]: r["id"] for r in services.list_pools(conn)}

    p_music = name_to_id["音乐喷泉"]
    p_mirror = name_to_id["镜面水池"]
    p_kids = name_to_id["戏水浅池"]

    def sched_for(pool_id):
        return conn.execute("SELECT id FROM schedules WHERE pool_id=? ORDER BY id LIMIT 1",
                            (pool_id,)).fetchone()["id"]

    now = services.now_dt()

    # 1) 水质超限：浊度 8.6 NTU，人工启动被拦截（含误启动留痕）
    adapters.set_scenario(p_kids, turbidity=8.6)
    r1 = services.attempt_start(conn, sched_for(p_kids),
                                operator="养护员-演示", manual=True, now=now)
    # 窗口标记为已结束，保证样例在独立服务进程中不被自动重试覆盖
    conn.execute("UPDATE schedules SET end_at=? WHERE pool_id=?",
                 (now.strftime("%H:%M"), p_kids))
    conn.commit()
    print(f"[1] 水质超限样例：戏水浅池启动拦截 -> {r1['status']} / {r1['reason']}")

    # 2) 天气停运：音乐喷泉正常启动后，雷暴紧急停运
    adapters.set_scenario(p_music)
    adapters.clear_scenarios()
    r2 = services.attempt_start(conn, sched_for(p_music), operator="scheduler", now=now)
    adapters.set_weather("storm", temp_c=21.0, wind_level=7)
    stopped = services.weather_stop_running(conn, now)
    adapters.set_weather("clear")
    conn.execute("UPDATE schedules SET end_at=? WHERE pool_id=?",
                 (now.strftime("%H:%M"), p_music))
    conn.commit()
    print(f"[2] 天气停运样例：音乐喷泉启动={r2['ok']}，雷暴停运水池={stopped}")

    # 3) 复开退回：镜面水池清池完成，养护员申请复开，未做复测/试运行 -> 主管退回
    c1 = services.start_clean(conn, p_mirror, keeper="养护员-演示", note="季度清池")
    services.finish_clean(conn, c1["task_id"], keeper="养护员-演示")
    req = services.request_reopen(conn, "clean", c1["task_id"], keeper="养护员-演示")
    r3 = services.approve_reopen(conn, req["request_id"],
                                 reviewer="主管-演示", role=config.ROLE_SUPERVISOR)
    conn.commit()
    print(f"[3] 复开退回样例：申请 {req['request_id']} -> {r3.get('reason')}")

    print("\n样例已生成。运行 `python -m fountain.cli serve` 后在"
          "“水质记录 / 运行日历 / 复开审批”页面查看。")
    conn.close()


def cmd_serve(args) -> None:
    config.ensure_dirs()
    conn = connect(args.db)
    init_db(conn)
    seed(conn)
    conn.close()

    from .web import server
    sched = scheduler.Scheduler(args.db, tick_seconds=args.tick)
    sched.start()
    httpd = server.make_server(args.host, args.port, args.db)
    url = f"http://{args.host}:{args.port}/"
    print(f"喷泉平台运行中：{url}（Ctrl+C 停止，调度 tick={args.tick}s）")
    if args.browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中…")
    finally:
        sched.stop()
        httpd.server_close()


def cmd_tick(args) -> None:
    conn = connect(args.db)
    init_db(conn)
    s = scheduler.Scheduler(args.db)
    import json
    print(json.dumps(s.tick(conn), ensure_ascii=False, indent=2))
    conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="公园喷泉启停与水质维护平台")
    p.add_argument("--db", default=None, help="SQLite 路径（默认 data/fountain.db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="建库")
    sp.add_argument("--seed", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("reset", help="重置并写入演示数据")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("demo-sample", help="生成超限/天气停运/复开退回样例")
    sp.set_defaults(func=cmd_demo_sample)

    sp = sub.add_parser("serve", help="启动调度器与 Web 平台")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--tick", type=int, default=config.TICK_SECONDS)
    sp.add_argument("--browser", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("tick", help="执行一次调度（调试）")
    sp.set_defaults(func=cmd_tick)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
