"""命令行：初始化数据库、载入 JSON、启动开发服务器。

示例：
    python -m app.cli init --db /tmp/f.db
    python -m app.cli load data/sample_data.json --db /tmp/f.db --replace
    DB_PATH=/tmp/f.db python -m app.cli serve --port 8000
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .db import connect, init_schema, resolve_db_path
from .loader import LoaderError, load_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE = PROJECT_ROOT / "data" / "sample_data.json"


def cmd_init(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    try:
        init_schema(conn)
        print(f"[ok] 已初始化数据库: {resolve_db_path(args.db)}")
    finally:
        conn.close()
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    try:
        init_schema(conn)
        try:
            summary = load_file(conn, args.file, replace=args.replace)
        except LoaderError as err:
            print("[error] JSON 校验/载入失败：", file=sys.stderr)
            for e in err.errors:
                print(f"  - {e}", file=sys.stderr)
            return 2
        print(f"[ok] 载入完成 ({'替换' if args.replace else '追加'})："
              f"作品 {summary['works']}，评委 {summary['judges']}，"
              f"回避 {summary['recusals']}，派单 {summary['assignments']}")
    finally:
        conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # 延迟导入，避免 --help 依赖 Flask 配置
    from . import create_app

    db_path = resolve_db_path(args.db)
    os.environ["DB_PATH"] = db_path
    app = create_app(db_path)

    # 本地零配置体验：空库时自动载入样例
    if args.sample_if_empty:
        conn = connect(db_path)
        try:
            init_schema(conn)
            n = conn.execute("SELECT COUNT(*) AS n FROM works").fetchone()["n"]
            if n == 0 and DEFAULT_SAMPLE.exists():
                summary = load_file(conn, DEFAULT_SAMPLE, replace=False)
                print(f"[ok] 空库自动载入样例：{summary}")
        finally:
            conn.close()

    print(f"[ok] 数据库: {db_path}")
    print(f"[ok] 打开 http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="影展评委派单/回避补位系统")
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 $DB_PATH 或 ./festival.db）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="建表")
    p_init.set_defaults(func=cmd_init)

    p_load = sub.add_parser("load", help="从 JSON 载入数据")
    p_load.add_argument("file", help="JSON 文件路径")
    p_load.add_argument("--replace", action="store_true",
                        help="先清空全部业务数据再载入")
    p_load.set_defaults(func=cmd_load)

    p_serve = sub.add_parser("serve", help="启动 Flask 开发服务器")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-sample", dest="sample_if_empty",
                        action="store_false",
                        help="空库时不自动载入样例")
    p_serve.set_defaults(func=cmd_serve, sample_if_empty=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
