"""既存の制御 API の起動前に B16b の検証を足す。"""
from __future__ import annotations


def build():
    from core.control.__main__ import build as base_build
    from core.intake.dest.b16b.common import install_control

    core, work, service = base_build()
    install_control(service)
    return core, work, service


def main():
    import asyncio
    from core.control import __main__ as control_main

    base_build = control_main.build

    def extended_build():
        from core.intake.dest.b16b.common import install_control
        core, work, service = base_build()
        install_control(service)
        return core, work, service

    control_main.build = extended_build
    asyncio.run(control_main.main())


if __name__ == "__main__":
    main()
