"""The engine mode, read in one place. The replica is kernel-only: the mode is always `kernel`.

Only the orchestrator reads it: `Dispatcher.handle_submit` calls `read()` once for each `dual.<op>` job and stores the answer in
the parent's payload (`engine_mode`); every other process learns the mode from the orchestrator (its /health, the payload).
`CAD_ENGINE` may still be set to `kernel`; any other value is ignored.
"""

MODES = ("kernel",)


def read():
    return "kernel"
