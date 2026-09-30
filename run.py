#!/usr/bin/env python3
"""本地启动入口：python3 run.py [--port 8000] [--no-scheduler]"""
import argparse

from app.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="公园喷泉启停与水质维护平台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-scheduler", action="store_true", help="关闭进程内定时任务")
    args = ap.parse_args()
    serve(args.host, args.port, with_scheduler=not args.no_scheduler)
