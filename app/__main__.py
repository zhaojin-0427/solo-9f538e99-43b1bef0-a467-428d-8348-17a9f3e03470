"""支持 ``python -m app`` 直接启动（等价于 python -m app.cli serve）。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(["serve"]))
