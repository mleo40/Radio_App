"""On-demand lifecycle management for backing radio applications.

JS8Call, WSJTX, Pat, and Mercury are external programs that Radio_App talks to
over TCP/HTTP but does not bundle.  Operators previously had to launch (and
juggle) these programs by hand; ProcManager adds explicit lifecycle control so
the TUI can start them on request, stop them cleanly on mode switch, and hand
the radio interlock to the incoming transport without collision.

Design invariants
-----------------
* ``is_running()`` always queries the OS process table (``pgrep -x``).  We never
  store a "this process is running" boolean — OS state is the truth.
* ``we_own()`` is distinct: it means WE spawned the process and hold the
  ``asyncio.subprocess.Process`` object, so WE are allowed to stop it.
* External processes (running before Radio_App started, or launched by the
  operator manually) are never killed on mode switch — we log and reconnect.
* Radio-contending transports (uses_shared_radio) trigger an interlock
  transfer on start so they don't key over each other.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TransportDef:
    """Static description of how to manage one transport's backing process."""

    default_cmd: str      # default shell command to launch (e.g. "pat http")
    process_name: str     # exact binary name for pgrep -x (e.g. "pat")
    contends_radio: bool = True  # True → drives the shared HF radio


# One entry per manageable transport.  Adding a new transport = one line here.
_TRANSPORT_DEFS: dict[str, TransportDef] = {
    "js8call": TransportDef("js8call",  "js8call", contends_radio=True),
    "wsjt_x":  TransportDef("wsjtx",    "wsjtx",   contends_radio=True),
    "winlink": TransportDef("pat http", "pat",      contends_radio=False),
}

# Maximum seconds to wait for a transport's check_reachable() to return OK
# after spawning its backing process.  Override per-transport via
# [transports.X].launch_wait_s in config.
_DEFAULT_WAIT_S = 15.0

# Seconds between reachability retry attempts while waiting for a process.
_POLL_INTERVAL_S = 0.5

# Prompt callback type: (transport_name, default_cmd) → custom_cmd | None
PromptFn = Callable[[str, str], Awaitable[str | None]]


def launch_default(name: str) -> str | None:
    """Return the built-in default launch command for a transport, or None."""
    defn = _TRANSPORT_DEFS.get(name)
    return defn.default_cmd if defn else None


class ProcManager:
    """Lifecycle manager for external radio application processes.

    Instantiated once in CoreApp alongside RadioInterlock. Inject into the TUI
    so /start can call start(); call stop_all() from App.stop() on shutdown.
    """

    def __init__(self, config, interlock) -> None:
        # config: radio_app.config.Config
        # interlock: RadioInterlock
        self._config = config
        self._interlock = interlock
        # name → asyncio.subprocess.Process (only processes WE started)
        self._owned: dict[str, asyncio.subprocess.Process] = {}

    # -- queries ---------------------------------------------------------------

    @staticmethod
    def known_transports() -> list[str]:
        return list(_TRANSPORT_DEFS)

    def definition(self, name: str) -> TransportDef | None:
        return _TRANSPORT_DEFS.get(name)

    def is_running(self, name: str) -> bool:
        """Query the OS process table — never uses stored state."""
        defn = _TRANSPORT_DEFS.get(name)
        if not defn:
            return False
        try:
            result = subprocess.run(
                ["pgrep", "-x", defn.process_name],
                capture_output=True,
                timeout=1.0,
            )
            return result.returncode == 0
        except FileNotFoundError:
            # pgrep not available (unlikely on Linux/macOS; stub on Windows)
            return False
        except subprocess.TimeoutExpired:
            return False

    def we_own(self, name: str) -> bool:
        """True if WE launched this process and it has not yet exited."""
        proc = self._owned.get(name)
        return proc is not None and proc.returncode is None

    # -- lifecycle -------------------------------------------------------------

    def _resolve_cmd(self, name: str) -> str:
        """Return the configured launch_cmd, or the default."""
        defn = _TRANSPORT_DEFS[name]
        configured = (
            self._config.transports.get(name, {}).get("launch_cmd", "") or ""
        ).strip()
        return configured if configured else defn.default_cmd

    def _wait_s(self, name: str) -> float:
        try:
            return float(
                self._config.transports.get(name, {}).get(
                    "launch_wait_s", _DEFAULT_WAIT_S
                )
            )
        except (TypeError, ValueError):
            return _DEFAULT_WAIT_S

    async def start(
        self,
        name: str,
        transport,
        prompt_fn: PromptFn | None = None,
    ) -> bool:
        """Start the backing process for ``name`` and connect the transport.

        Steps:
        1. Resolve launch command from config or default.
        2. If not configured and prompt_fn provided, ask the operator.
        3. If already running externally: skip spawn, attempt transport.start().
        4. Stop owned radio-contending processes if new transport also contends.
        5. Transfer interlock to new transport (if it contends the radio).
        6. Spawn process (start_new_session so it survives TUI resize signals).
        7. Poll transport.check_reachable() up to launch_wait_s.
        8. Call transport.start(); record ownership.

        Returns True on success, False on timeout or spawn error.
        """
        if name not in _TRANSPORT_DEFS:
            log.warning("proc_manager: unknown transport %r", name)
            return False

        cmd = self._resolve_cmd(name)

        # Step 2 — prompt for custom command if none configured
        if not self._config.transports.get(name, {}).get("launch_cmd", "").strip():
            if prompt_fn is not None:
                custom = await prompt_fn(name, cmd)
                if custom and custom.strip() and custom.strip() != cmd:
                    cmd = custom.strip()
                    # Persist so subsequent /start skips the prompt
                    self._config.set("transports", name, {
                        **self._config.transports.get(name, {}),
                        "launch_cmd": cmd,
                    })
                    try:
                        self._config.save()
                    except Exception:  # noqa: BLE001
                        log.warning("proc_manager: could not save launch_cmd for %s", name)

        # Step 3 — already running (externally)?
        if self.is_running(name) and not self.we_own(name):
            log.info(
                "proc_manager: %s is already running (externally — not managed). "
                "Reconnecting transport.",
                name,
            )
            try:
                await transport.start()
            except Exception:  # noqa: BLE001
                log.exception("proc_manager: transport.start() failed for %s", name)
                return False
            return True

        defn = _TRANSPORT_DEFS[name]

        # Check whether the transport actually contends the radio at runtime
        # (e.g. Winlink only contends when using an RF path, not telnet).
        new_contends = defn.contends_radio
        try:
            new_contends = transport.capabilities().uses_shared_radio
        except Exception:  # noqa: BLE001
            pass

        # Step 4 — stop owned radio-contending processes before switching
        if new_contends:
            for other_name, other_proc in list(self._owned.items()):
                if other_name == name:
                    continue
                if other_proc.returncode is not None:
                    continue
                other_def = _TRANSPORT_DEFS.get(other_name)
                if other_def and other_def.contends_radio:
                    log.info(
                        "proc_manager: stopping %s (owned, radio-contending) to free radio for %s",
                        other_name,
                        name,
                    )
                    await self.stop(other_name)

        # Step 5 — transfer interlock
        if new_contends:
            prev = self._interlock.transfer(name)
            if prev:
                log.debug("proc_manager: interlock transferred from %s to %s", prev, name)
        else:
            # Moving to a non-radio transport → release whoever had the radio
            self._interlock.transfer(name)

        # Step 6 — spawn
        if self.we_own(name):
            # Already owned and running — just reconnect the transport adapter
            log.debug("proc_manager: %s already owned and running; reconnecting", name)
        else:
            argv = shlex.split(cmd)
            log.info("proc_manager: spawning %s → %r", name, argv)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                log.error(
                    "proc_manager: could not find executable for %s (%r). "
                    "Set [transports.%s].launch_cmd in your config.",
                    name, cmd, name,
                )
                return False
            except Exception:  # noqa: BLE001
                log.exception("proc_manager: failed to spawn %s", name)
                return False
            self._owned[name] = proc

        # Step 7 — poll check_reachable() up to launch_wait_s
        from ..transports.base import ReachabilityStatus
        wait = self._wait_s(name)
        deadline = asyncio.get_event_loop().time() + wait
        reached = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                status = await transport.check_reachable()
                if status is ReachabilityStatus.OK:
                    reached = True
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(_POLL_INTERVAL_S)

        if not reached:
            log.error(
                "proc_manager: %s did not become reachable within %.0fs",
                name, wait,
            )
            return False

        # Step 8 — connect the transport adapter
        try:
            await transport.start()
        except Exception:  # noqa: BLE001
            log.exception("proc_manager: transport.start() failed for %s", name)
            return False

        log.info("proc_manager: %s started and reachable", name)
        return True

    async def stop(self, name: str, transport=None) -> None:
        """Stop an owned process and disconnect the transport adapter.

        Only acts if we_own(name); external processes are never killed.
        Calls transport.stop() before signalling the process so the transport
        adapter can close its socket/session cleanly.
        """
        if not self.we_own(name):
            log.debug("proc_manager: not stopping %s (not owned or already exited)", name)
            return

        if transport is not None:
            try:
                await transport.stop()
            except Exception:  # noqa: BLE001
                log.exception("proc_manager: transport.stop() failed for %s", name)

        proc = self._owned.pop(name)
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("proc_manager: %s did not exit cleanly; sending SIGKILL", name)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("proc_manager: %s still running after SIGKILL", name)

    async def stop_all(self) -> None:
        """Stop every owned process.  Called from App.stop() on shutdown."""
        for name in list(self._owned):
            await self.stop(name)
