"""兼容旧入口：实际逻辑在 fsm.recap。"""
from fsm.recap import main

if __name__ == "__main__":
    raise SystemExit(main())
