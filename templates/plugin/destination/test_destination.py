"""Copy to a bundle test directory after placing the destination package."""
from __future__ import annotations

import importlib
from types import SimpleNamespace


def test_example_destination_registers_an_in_process_handler():
    package = importlib.import_module("core.intake.dest.example_destination")
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(dest_handlers={})))

    package.register(app)
    status, result = app.state.intake.dest_handlers["example.echo"](
        app,
        {"name": "example.echo"},
        {"payload": {"message": "hello"}},
        None,
    )

    assert status == 200
    assert result == {"message": "hello"}
