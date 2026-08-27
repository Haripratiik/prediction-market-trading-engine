"""Kill switch.  T-043.  PLAN.md I9 and 10.6.

    I9  "A KILL file in the run dir cancels everything, from any state, within
         5 seconds."
    10.6 `touch KILL` -> cancel all orders on all venues, refuse new orders, page.
         Recovery requires removing the file AND a successful reconciliation.

Four design choices make that promise true, and none of them is cosmetic:

  * **The file IS the state.**  `is_engaged()` stats the path on every call --
    no cache, no in-process boolean.  An operator typing `touch KILL` in the run
    directory is therefore seen by the very next check, from any process state,
    including from inside a placement loop that never yields.
  * **Engage BEFORE cancelling.**  `panic()` writes the file first and only then
    talks to the venue.  If the cancel round-trip hangs or the venue is down, the
    file is already on disk, so every executor in the process refuses new sends
    regardless of what the network does.  A kill that depends on a successful
    network call is not a kill.
  * **Nothing escapes the kill path.**  A venue that raises must not stop the
    remaining venues from being cancelled, so failures are recorded on
    `last_panic` for the monitor to page on rather than thrown.  `PanicResult.ok`
    is the signal; silence is not.
  * **Engage is create-if-absent, never overwrite.**  Repeated calls are safe and
    the FIRST reason survives -- that is the one the incident review needs.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.models import from_us, now_us

# The name PLAN.md 10.6 tells the operator to `touch`.  Do not make it
# configurable per-deployment: a runbook the operator has to look up is a runbook
# that gets typed wrong at 3am.
KILL_FILENAME = "KILL"

# I9's contract with the operator.  Everything in the kill path is bounded by it.
KILL_DEADLINE_S = 5.0


@runtime_checkable
class Cancellable(Protocol):
    """The one method the kill path needs.  `KalshiClient` satisfies it."""

    def cancel_all_orders(self) -> int: ...


class KillEngaged(RuntimeError):
    """Raised when an action is attempted while the kill switch is engaged."""


@dataclass(frozen=True, slots=True)
class PanicResult:
    """What one `panic()` actually achieved.  Read `ok`, do not assume it."""

    cancelled: int
    elapsed_s: float
    attempts: int
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def within_deadline(self) -> bool:
        return self.elapsed_s <= KILL_DEADLINE_S

    def as_dict(self) -> dict[str, Any]:
        return {
            "cancelled": self.cancelled,
            "elapsed_s": round(self.elapsed_s, 4),
            "attempts": self.attempts,
            "errors": list(self.errors),
            "ok": self.ok,
            "within_deadline": self.within_deadline,
        }


class KillSwitch:
    """I9.  A file on disk, because a file survives what a process does not.

    In-memory flags die with the process that holds them and are invisible to an
    operator with a shell.  A file is visible to both, needs no IPC, and is the
    one mechanism that still works when the trading loop is wedged.
    """

    def __init__(
        self,
        run_dir: str | Path = ".",
        *,
        filename: str = KILL_FILENAME,
        deadline_s: float = KILL_DEADLINE_S,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / filename
        self.deadline_s = deadline_s
        self.last_panic: PanicResult | None = None

    # ------------------------------------------------------------------ state
    def is_engaged(self) -> bool:
        """Stat the path.  Deliberately uncached -- see the module docstring."""
        return self.path.exists()

    def reason(self) -> str | None:
        """Why the switch is engaged, or None if it is not."""
        try:
            body = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        _, _, rest = body.partition("\t")
        return (rest or body).strip() or "(no reason recorded)"

    def engaged_at_us(self) -> int | None:
        try:
            head, _, _ = self.path.read_text(encoding="utf-8").partition("\t")
            return int(head.strip())
        except (OSError, ValueError):
            return None

    def require_clear(self) -> None:
        """Raise unless trading is permitted.  Call before any send."""
        if self.is_engaged():
            raise KillEngaged(f"KILL file at {self.path}: {self.reason()}")

    # ----------------------------------------------------------- engage/clear
    def engage(self, reason: str = "manual") -> Path:
        """Create the KILL file.  Idempotent; the FIRST reason is preserved.

        `x` mode makes this atomic against another process (or another thread)
        engaging at the same instant -- both calls succeed, one file exists, and
        the reason recorded is whichever landed first.  That is the reason the
        incident review wants; a later overwrite would bury the trigger under
        whatever cascaded from it.
        """
        try:
            with open(self.path, "x", encoding="utf-8") as fh:
                fh.write(f"{now_us()}\t{reason}\n")
        except FileExistsError:
            pass
        except OSError:
            # Disk full / read-only run dir.  The caller still needs the kill to
            # take effect, so fall back to a plain write and let a truly
            # unwritable directory surface as the OSError it is.
            self.path.write_text(f"{now_us()}\t{reason}\n", encoding="utf-8")
        return self.path

    def disengage(self) -> bool:
        """Remove the KILL file.  Returns whether one was there.

        This is HALF of recovery.  PLAN.md 10.6 also requires a successful
        reconciliation -- use `recover()` for the operator-facing path; this bare
        form exists for tooling and tests.
        """
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return existed

    def recover(self, drift_report: Any, *, note: str = "") -> bool:
        """The 10.6 recovery path: clean reconciliation THEN remove the file.

        Recovering on the file alone is how a process resumes quoting on top of
        state it no longer agrees with the venue about -- which is the failure
        the kill switch was engaged to stop in the first place.

        `drift_report` is anything exposing `is_clean` (execution.oms.DriftReport).
        """
        clean = bool(getattr(drift_report, "is_clean", False))
        if not clean:
            raise KillEngaged(
                f"refusing to clear {self.path}: reconciliation is not clean "
                f"({drift_report}){' -- ' + note if note else ''}"
            )
        return self.disengage()

    # ------------------------------------------------------------------ panic
    def panic(
        self,
        client: Cancellable,
        *,
        reason: str = "panic",
        deadline_s: float | None = None,
        attempts: int = 3,
    ) -> int:
        """Cancel everything at `client`.  Returns the number cancelled.

        Engages first, so the refusal of NEW orders is already in force before a
        single byte goes over the wire (I9).  Retries a failing venue only while
        the I9 budget allows; a cancel that has not landed inside the deadline is
        an incident to page on, not something to keep grinding at.

        Never raises: `last_panic.ok` reports the truth, so one broken venue
        cannot abort the cancellation of the others.
        """
        self.engage(reason)
        budget = self.deadline_s if deadline_s is None else deadline_s
        started = time.monotonic()
        cancelled = 0
        errors: list[str] = []
        tries = 0
        while True:
            tries += 1
            try:
                cancelled += int(client.cancel_all_orders() or 0)
                break
            except Exception as exc:                       # noqa: BLE001 -- see docstring
                errors.append(f"{type(exc).__name__}: {exc}")
            if tries >= attempts or (time.monotonic() - started) >= budget:
                break
        self.last_panic = PanicResult(
            cancelled=cancelled,
            elapsed_s=time.monotonic() - started,
            attempts=tries,
            errors=tuple(errors),
        )
        return cancelled

    def panic_all(self, clients: Iterable[Cancellable], *, reason: str = "panic") -> int:
        """I9 says all orders on ALL venues.  One failure must not stop the rest."""
        total = 0
        results: list[PanicResult] = []
        for c in clients:
            total += self.panic(c, reason=reason)
            if self.last_panic is not None:
                results.append(self.last_panic)
        self.last_panic = PanicResult(
            cancelled=total,
            elapsed_s=sum(r.elapsed_s for r in results),
            attempts=sum(r.attempts for r in results),
            errors=tuple(e for r in results for e in r.errors),
        )
        return total

    # ---------------------------------------------------------------- context
    @contextmanager
    def armed_context(
        self,
        client: Cancellable | None = None,
        *,
        reason: str = "unhandled exception",
    ) -> Iterator["KillSwitch"]:
        """Engage on ANY unhandled exception leaving the block.

        Catches `BaseException`, not `Exception`: a `KeyboardInterrupt` or a
        `SystemExit` out of a quoting loop leaves resting orders behind exactly
        like a crash does, and the operator who pressed Ctrl-C wants the same
        outcome as the operator who touched KILL.  The exception is re-raised --
        this arms the switch, it does not swallow the failure.
        """
        try:
            yield self
        except BaseException as exc:
            self.engage(f"{reason}: {type(exc).__name__}: {exc}")
            if client is not None:
                # engage() above already fired; panic()'s own engage is a no-op.
                self.panic(client, reason="armed_context")
            raise

    # ----------------------------------------------------------------- pretty
    def describe(self) -> str:
        if not self.is_engaged():
            return f"clear ({self.path})"
        at = self.engaged_at_us()
        when = from_us(at).isoformat() if at else "unknown time"
        return f"ENGAGED at {when}: {self.reason()}"

    def __repr__(self) -> str:
        return f"KillSwitch(path={str(self.path)!r}, engaged={self.is_engaged()})"
