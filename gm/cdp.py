"""Safe CDP command helpers.

SeleniumBase's Connection.send() swallows protocol errors by *closing the tab's
websocket* (which drops Page.enable / setBypassCSP / our injected script and
strands every other in-flight command). These helpers send commands on the same
socket but raise errors to the caller and always clean up after timeouts.
"""
from __future__ import annotations

import asyncio
import itertools

import mycdp as cdp
from websockets.protocol import State
from seleniumbase.undetected.cdp_driver.connection import Transaction


class CDPError(RuntimeError):
    pass


class JSError(RuntimeError):
    pass


def socket_id(tab) -> int:
    """Identity of the tab's current DevTools session (changes after a reconnect)."""
    return id(getattr(tab, "websocket", None))


async def call(tab, cmd, timeout: float = 15):
    """Send one CDP command on `tab` and return its parsed result (raises on error/timeout)."""
    await asyncio.wait_for(tab.aopen(), 10)
    ws = tab.websocket
    if ws is None or ws.state is State.CLOSED:
        raise CDPError("tab connection is closed")
    tx = Transaction(cmd)
    tx.connection = tab
    # Our own id range, never reset: a late reply to a timed-out command must not be
    # delivered to a newer command that reused its id (the library restarts at 0).
    ids = tab.__dict__.get("_gm_ids")
    if ids is None:
        ids = tab.__dict__["_gm_ids"] = itertools.count(1_000_000)
    tx.id = next(ids)
    tab.mapper[tx.id] = tx
    try:
        await ws.send(tx.message)
        return await asyncio.wait_for(tx, timeout)
    except asyncio.TimeoutError:
        raise CDPError(f"{tx.method} timed out after {timeout:.0f}s") from None
    except asyncio.CancelledError:
        raise
    except Exception as e:  # ProtocolException etc.
        raise CDPError(f"{tx.method}: {e}") from None
    finally:
        # a late reply to a cancelled future would crash the library's listener
        tab.mapper.pop(tx.id, None)


async def evaluate(tab, expression: str, timeout: float = 20, await_promise: bool = False):
    """Runtime.evaluate in the page's main world; returns the JSON value or raises JSError."""
    remote, exc = await call(tab, cdp.runtime.evaluate(
        expression=expression, return_by_value=True, await_promise=await_promise,
        allow_unsafe_eval_blocked_by_csp=True), timeout)
    if exc is not None:
        detail = ""
        try:
            detail = exc.exception.description or ""
        except Exception:
            pass
        raise JSError((detail or exc.text or "script error").splitlines()[0][:300])
    return None if remote is None else remote.value
