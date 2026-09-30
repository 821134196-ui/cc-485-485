"""一条本地命令生成三类样例：超限拦截、天气停运、复开退回。

用法：
    python3 -m app.demo            # 在 data/fountain.db 生成样例
    python3 run.py                 # 随后启动平台查看
"""
from datetime import datetime

from .adapters import adapter
from .database import connect, init_db
from .scheduler import Scheduler
from .services import PlatformService, ServiceError, PermissionError_, config


def run(verbose: bool = True) -> dict:
    conn = connect()
    init_db(conn)
    svc = PlatformService(conn)
    sched = Scheduler(svc)
    adapter.reset()

    def log(msg):
        if verbose:
            print(msg)

    log("== 重置模拟源，样例 1 首次 tick 将完成采样与故障登记 ==")

    # ---------- 样例 1：水质超限 → 到点启动被拦截 ----------
    log("\n[1/3] 水质超限样例（中央主水池）")
    adapter.set_scenario(1, "quality_over")
    blocked = svc.start_pool(1, "计划调度", schedule_id=1)
    sched.tick()  # 产生超限自动读数并登记水质故障
    log(f"      启动结果：{'放行' if blocked['ok'] else '拦截'}，原因："
        f"{'、'.join(blocked.get('precheck', {}).get('reason_texts', []))}")
    adapter.set_scenario(1, "normal")  # 恢复模拟源，故障单保留为待处置样例

    # ---------- 样例 2：天气停运 → 运行中水池紧急停运，预警解除自动闭环 ----------
    log("\n[2/3] 天气停运样例（北门圆形水池）")
    svc.stop_pool(2, "系统", "重置演示状态")
    started = svc.start_pool(2, "计划调度", schedule_id=3)
    log(f"      预警前启动：{'成功' if started['ok'] else '失败'}")
    adapter.set_weather(True)
    sched.tick()  # 天气故障边沿：紧急停运
    log("      气象预警接入：运行中水池已紧急停运，原因记入运行日志")
    adapter.set_weather(False)
    sched.tick()  # 预警解除：天气故障自动闭环
    log("      预警解除：天气故障自动闭环，历史停运原因保留在日历/日志中")

    # ---------- 样例 3：清池完工复开 → 主管退回 ----------
    log("\n[3/3] 复开退回样例（东侧静水池）")
    today = datetime.now().strftime("%Y-%m-%d")
    cid = svc.create_cleaning(3, today, "养护员·李工")
    svc.lock_cleaning(cid, "养护员·李工")
    svc.start_cleaning(cid, "养护员·李工")
    rid = svc.finish_cleaning(cid, "养护员·李工")
    # 复测数据偏高、喷头试运行正常
    svc.add_reading(3, {"level": 0.74, "turbidity": 7.8, "chlorine": 0.52},
                    kind="retest", actor="养护员·李工", note="清池后首次复测")
    trial = svc.nozzle_trial(3, "养护员·李工")
    log(f"      清池 #{cid} 完工，复开申请 #{rid}；复测浊度 7.8NTU，"
        f"喷头试运行{'正常' if trial['ok'] else '异常'}")
    try:
        svc.decide_reopen(rid, "reject", "主管·王敏", config.ROLE_SUPERVISOR,
                          "复测浊度仍超限，重新投加絮凝剂并复测后再申请")
    except (ServiceError, PermissionError_) as e:
        log(f"      退回失败：{e}")
    log("      主管审批：退回，水池继续锁定，可在『复开审批』页补齐材料后重新申请")

    log("\n样例生成完毕。启动平台查看：python3 run.py  →  http://127.0.0.1:8000")
    return {"blocked": not blocked["ok"], "reopen_request_id": rid, "cleaning_id": cid}


if __name__ == "__main__":
    run()
