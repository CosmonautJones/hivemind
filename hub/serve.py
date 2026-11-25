"""Hub entry point: `python hub/serve.py` (used by watchdog/run-hub.cmd).

On Windows we force the selector event loop. The default IOCP proactor loop can lose its listening
socket on some machines/drivers (seen on Python 3.14: `OSError: [WinError 64] The specified network
name is no longer available` in accept), which leaves the hub up but unreachable.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main() -> None:
    if sys.platform == "win32":
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # policy API deprecated in 3.14, still works
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import uvicorn
    port = int(os.environ.get("FORGE_PORT", "8787"))
    # loop="asyncio" + no subprocess: uvicorn keeps the policy set above
    uvicorn.run("app:app", host="0.0.0.0", port=port, loop="asyncio", access_log=False)


if __name__ == "__main__":
    main()
