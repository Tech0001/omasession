#!/usr/bin/env python3
"""Repair Chromium-family window placement after an application restart.

The normal replay is allowed to launch and close windows because it runs on a
desktop being brought back after login. This path is different: it runs from
the existing snapshot save while the desktop is live. It only moves windows
that already exist. Automatic runs wait for a browser process generation
change; an explicit app-only run can force the same validated placement.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay as R  # noqa: E402
from safe_fs import (  # noqa: E402
    Refused,
    TooLarge,
    open_dir_chain,
    read_bytes,
    write_bytes,
)


STATE_NAME = "browser-repair.json"
REPAIR_TIMEOUT = 8.0
REPAIR_QUIET = 1.0
GIVE_UP_AFTER = 90.0
REPAIR_RUN_BUDGET = 50.0
BROWSER_CLASSES = frozenset(R.BROWSERS)
CLIENTS_TIMEOUT = 2


def mapped_clients(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [
        c for c in value
        if isinstance(c, dict)
        and c.get("mapped")
        and isinstance(c.get("workspace"), dict)
        and c["workspace"].get("id", 0) > 0
    ]


def proc_starttime(pid: int) -> str | None:
    """Read Linux /proc stat field 22 without splitting a parenthesized comm."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        close = raw.rfind(")")
        if close < 0:
            return None
        fields = raw[close + 2 :].split()
        # The first item after the comm is field 3; field 22 is index 19.
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def client_identity(client: dict) -> tuple[str, str, str] | None:
    try:
        pid = int(client.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    start = proc_starttime(pid)
    if start is None:
        return None
    return str(pid), start, str(client.get("class", ""))


def identity_for_clients(clients: list[dict], app: str) -> tuple[str, ...]:
    identities: set[str] = set()
    for client in clients:
        if client.get("class") != app:
            continue
        identity = client_identity(client)
        if identity is not None:
            identities.add(f"{identity[0]}:{identity[1]}")
    return tuple(sorted(identities))


def current_browser_clients(clients: list[dict] | None, app: str) -> list[dict]:
    if not isinstance(clients, list):
        return []
    return [c for c in clients if c.get("class") == app]


def same_window_identity(expected: dict, observed: dict) -> bool:
    """Keep an address from being reused for a changed browser window."""
    expected_identity = expected.get("_identity")
    return (
        observed.get("address") == expected.get("address")
        and observed.get("title") == expected.get("title")
        and observed.get("class") == expected.get("class")
        and expected_identity is not None
        and client_identity(observed) == expected_identity
    )


def same_window_source(expected: dict, observed: dict) -> bool:
    """Validate the complete source state immediately before a move."""
    return same_window_identity(expected, observed) and str(
        observed.get("workspace", {}).get("id", "")
    ) == str(expected.get("workspace", {}).get("id", ""))


def browser_layout(windows: list[dict], titles: list[dict]) -> dict[str, list[dict]]:
    """Keep the browser part of a coherent snapshot independent of `last`."""
    out: dict[str, list[dict]] = {}
    for title in titles:
        app = title.get("class")
        if app in BROWSER_CLASSES:
            out.setdefault(app, []).append(dict(title))
    # A title sidecar may be absent for a partial/legacy pair.  Do not invent
    # titles, but keep a visible app out of the state rather than pretending it
    # can be matched after a restart.
    return out


def match_plan(current: list[dict], saved: list[dict]) -> list[tuple[dict, dict]]:
    """Return one-to-one matches; reserve exact titles before fuzzy ones."""
    candidates = [dict(item) for item in saved]
    matches: list[tuple[dict, dict]] = []
    fuzzy: list[dict] = []
    for window in current:
        exact = [
            i for i, candidate in enumerate(candidates)
            if candidate.get("title") == window.get("title", "")
        ]
        # Consume every exact slot before fuzzy matching. Equal titles are
        # indistinguishable, but they still reserve their saved destinations;
        # a fuzzy title must never steal one of those slots.
        if exact:
            matches.append((window, candidates.pop(exact[0])))
        else:
            fuzzy.append(window)
    for window in fuzzy:
        target = R.take_best_match(window.get("title", ""), candidates)
        if target is None:
            continue
        matches.append((window, target))
    return matches


def repair_plan(current: list[dict], saved: list[dict]) -> list[tuple[dict, dict]]:
    """Return one-to-one moves; already-correct windows count as matched."""
    plan: list[tuple[dict, dict]] = []
    for window, target in match_plan(current, saved):
        current_ws = str(window.get("workspace", {}).get("id", ""))
        if current_ws != str(target.get("workspace", "")):
            plan.append((window, target))
    return plan


def read_clients() -> list[dict] | None:
    try:
        out = subprocess.run(
            ["hyprctl", "clients", "-j"],
            capture_output=True,
            text=True,
            timeout=CLIENTS_TIMEOUT,
        )
        if out.returncode != 0:
            return None
        return mapped_clients(json.loads(out.stdout))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def read_state(state_dir: str) -> dict:
    fd = open_dir_chain(state_dir, create=True)
    try:
        raw = read_bytes(fd, STATE_NAME)
    finally:
        os.close(fd)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("browser repair state does not parse")
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("browser repair state has an unsupported version")
    return value


def write_state(state_dir: str, state: dict) -> None:
    payload = (json.dumps(state, sort_keys=True) + "\n").encode()
    fd = open_dir_chain(state_dir, create=True)
    try:
        write_bytes(fd, STATE_NAME, payload, mode=0o600)
    finally:
        os.close(fd)


def persist_state(state_dir: str, state: dict) -> bool:
    try:
        write_state(state_dir, state)
    except (OSError, Refused, TooLarge) as exc:
        print(f"[browser-repair] refusing unsafe state: {exc}", file=sys.stderr)
        return False
    return True


def load_saved_layout(session: Path) -> dict[str, list[dict]]:
    if not session.is_file():
        return {}
    windows, titles, _ = R.load_pair(session)
    if not windows or not titles:
        return {}
    return browser_layout(windows, titles)


def boot_identity() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def new_state() -> dict:
    return {
        "version": 1,
        "boot_id": boot_identity(),
        "hyprland": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""),
        "browsers": {},
        "layout": {},
        "pending": {},
    }


def same_session(state: dict) -> bool:
    return (
        state.get("boot_id") == boot_identity()
        and state.get("hyprland", "")
        == os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    )


def refresh_layout(state: dict, layout: dict[str, list[dict]]) -> None:
    for app, entries in layout.items():
        if entries:
            state.setdefault("layout", {})[app] = entries


def process_app(
    app: str,
    clients: list[dict],
    state: dict,
    disabled: bool,
    dry_run: bool,
    run_deadline: float | None = None,
    force: bool = False,
) -> tuple[bool, bool]:
    """Return (changed, pending)."""
    current = current_browser_clients(clients, app)
    identity = identity_for_clients(current, app)
    previous = tuple(state.get("browsers", {}).get(app, {}).get("identity", []))

    if not identity:
        # Keep the last identity and layout while the browser is closed.  A
        # later process with a different generation will still be detected.
        return False, False
    if not force and not previous:
        state.setdefault("browsers", {})[app] = {"identity": list(identity)}
        return False, False
    if not force and (tuple(identity) == previous or disabled):
        state.setdefault("browsers", {})[app] = {"identity": list(identity)}
        state.setdefault("pending", {}).pop(app, None)
        return False, False
    # Both automatic and explicit runs require exactly one live browser
    # generation. The manual action intentionally does not require a previous
    # token; that is what lets it repair a browser whose process stayed alive.
    if len(identity) != 1 or (not force and len(previous) != 1):
        print(f"[browser-repair] {app}: ambiguous process identity; preserving layout")
        return False, False

    saved = state.get("layout", {}).get(app, [])
    if not saved:
        state.setdefault("browsers", {})[app] = {"identity": list(identity)}
        state.setdefault("pending", {}).pop(app, None)
        return False, False

    pending = state.setdefault("pending", {}).get(app)
    now = time.time()
    if not isinstance(pending, dict) or pending.get("identity") != list(identity):
        pending = {"identity": list(identity), "since": now}
        state["pending"][app] = pending

    latest = current
    deadline = time.monotonic() + REPAIR_TIMEOUT
    if run_deadline is not None:
        deadline = min(deadline, run_deadline)
    last_change = time.monotonic()
    best_matches: list[tuple[dict, dict]] = []
    while True:
        matches = match_plan(latest, saved)
        if len(matches) > len(best_matches):
            best_matches = matches
            last_change = time.monotonic()
        if best_matches and len(best_matches) >= len(saved):
            break
        if time.monotonic() >= deadline:
            break
        # A browser can expose its first window before restoring the rest. Do
        # not declare the new process stable just because that first window is
        # already matchable; wait for the saved set or the bounded deadline.
        if best_matches and time.monotonic() - last_change >= REPAIR_QUIET and (
            force or len(latest) >= len(saved)
        ):
            break
        time.sleep(0.25)
        # Never let another application consume a browser title while the
        # browser is still restoring. The initial sample is filtered too;
        # keep that invariant for every later compositor read.
        observed = read_clients()
        latest = current_browser_clients(observed, app)

    # The last read is authoritative. A plan from an earlier poll may have
    # matched a transient title that has since changed, so never dispatch that
    # stale plan. A transport failure is also not evidence that the browser has
    # no matching windows; keep the old snapshot and retry.
    final_observed = read_clients()
    if final_observed is None:
        print(f"[browser-repair] {app}: cannot verify current browser windows")
        return False, True
    latest = current_browser_clients(final_observed, app)
    if identity_for_clients(latest, app) != identity:
        print(f"[browser-repair] {app}: browser generation changed while waiting")
        return False, True
    final_matches = match_plan(latest, saved)
    complete = len(final_matches) >= len(saved)
    if not force and not complete and now - float(pending.get("since", now)) < GIVE_UP_AFTER:
        print(f"[browser-repair] {app}: restart detected, waiting for titled windows")
        return False, True
    if not final_matches:
        if force:
            print(f"[browser-repair] {app}: no saved title matched; no window moved")
        else:
            print(f"[browser-repair] {app}: no saved title matched after 90s; adopting new layout")
        state.setdefault("browsers", {})[app] = {"identity": list(identity)}
        state.setdefault("pending", {}).pop(app, None)
        return False, False

    best_matches = final_matches

    best_plan = [
        (window, target)
    for window, target in best_matches
        if str(window.get("workspace", {}).get("id", ""))
        != str(target.get("workspace", ""))
    ]
    best_plan = [
        (dict(window, _identity=client_identity(window)), target)
        for window, target in best_plan
    ]
    moved = 0
    if force and not complete:
        print(
            f"[browser-repair] {app}: applying {len(final_matches)} matched window(s); "
            f"{len(saved) - len(final_matches)} saved window(s) not open"
        )
    for window, target in best_plan:
        if run_deadline is not None and time.monotonic() >= run_deadline:
            print(f"[browser-repair] {app}: repair budget exhausted; retrying next tick")
            return False, True
        if dry_run:
            print(
                f"[browser-repair] {app}: {window.get('title', '')!r} "
                f"→ ws{target.get('workspace')}"
            )
        else:
            observed = read_clients()
            if observed is None:
                print(
                    f"[browser-repair] {app}: could not verify "
                    f"{window.get('title', '')!r} before move"
                )
                return False, True
            by_address = {item.get("address"): item for item in observed}
            live = by_address.get(window.get("address"))
            if live is None or not same_window_source(window, live):
                print(
                    f"[browser-repair] {app}: plan expired for "
                    f"{window.get('title', '')!r}; {moved} window(s) already "
                    "moved, stopping remaining moves"
                )
                return bool(moved), True
            R.move_to(window["address"], target.get("workspace"))
            moved += 1
        if run_deadline is not None and time.monotonic() >= run_deadline:
            print(f"[browser-repair] {app}: repair budget exhausted; retrying next tick")
            return False, True
    if best_plan and not dry_run:
        verified = current_browser_clients(read_clients(), app)
        if identity_for_clients(verified, app) != identity:
            print(f"[browser-repair] {app}: browser generation changed during move")
            return False, True
        by_address = {item.get("address"): item for item in verified}
        failed = [
            window.get("title", "")
            for window, target in best_plan
            if (
                by_address.get(window.get("address")) is None
                or not same_window_identity(
                    window, by_address.get(window.get("address"), {})
                )
                or str(
                    by_address.get(window.get("address"), {})
                    .get("workspace", {})
                    .get("id", "")
                )
                != str(target.get("workspace", ""))
            )
        ]
        if failed:
            print(
                f"[browser-repair] {app}: could not verify placement for "
                + ", ".join(repr(title) for title in failed)
            )
            return False, True
    state.setdefault("browsers", {})[app] = {"identity": list(identity)}
    state.setdefault("pending", {}).pop(app, None)
    print(f"[browser-repair] {app}: moved {len(best_plan)} existing window(s)")
    return bool(best_plan), False


def run(
    session: Path,
    state_dir: str,
    clients: list[dict],
    disabled: bool,
    dry_run: bool,
    manual: bool = False,
    app: str | None = None,
    skip_apps: set[str] | None = None,
) -> int:
    try:
        state = read_state(state_dir)
    except (OSError, ValueError, Refused, TooLarge) as exc:
        print(f"[browser-repair] refusing unsafe state: {exc}", file=sys.stderr)
        return 3
    if not state:
        state = new_state()
        try:
            state["layout"] = load_saved_layout(session)
        except (OSError, ValueError) as exc:
            print(f"[browser-repair] refusing unreadable session: {exc}", file=sys.stderr)
            return 3
        for candidate in BROWSER_CLASSES:
            identity = identity_for_clients(clients, candidate)
            if identity:
                state["browsers"][candidate] = {"identity": list(identity)}
        if not manual:
            return 0 if persist_state(state_dir, state) else 3
    if not same_session(state):
        state = new_state()
        try:
            state["layout"] = load_saved_layout(session)
        except (OSError, ValueError) as exc:
            print(f"[browser-repair] refusing unreadable session: {exc}", file=sys.stderr)
            return 3
        for candidate in BROWSER_CLASSES:
            identity = identity_for_clients(clients, candidate)
            if identity:
                state["browsers"][candidate] = {"identity": list(identity)}
        if not manual:
            return 0 if persist_state(state_dir, state) else 3

    # The last coherent snapshot is the reference for a restart.  Keeping it
    # in this state file means a browser can be closed for several ticks
    # without an app-only save erasing the layout needed when it reopens.
    try:
        refresh_layout(state, load_saved_layout(session))
    except (OSError, ValueError) as exc:
        print(f"[browser-repair] refusing unreadable session: {exc}", file=sys.stderr)
        return 3
    pending = False
    run_deadline = time.monotonic() + REPAIR_RUN_BUDGET
    skipped = skip_apps or set()
    apps = [app] if app is not None else [
        candidate for candidate in sorted(BROWSER_CLASSES) if candidate not in skipped
    ]
    for candidate in apps:
        _, app_pending = process_app(
            candidate,
            clients,
            state,
            disabled,
            dry_run,
            run_deadline=run_deadline,
            force=manual,
        )
        pending = pending or app_pending
        if app_pending or time.monotonic() >= run_deadline:
            break

    # The shell will reread the compositor immediately after this helper. If a
    # different browser generation appeared while another app was being
    # repaired, refuse this tick so that reread cannot publish a flattened
    # session before the new generation gets its own repair attempt.
    if not pending and not disabled:
        observed = read_clients()
        if observed is None:
            print("[browser-repair] cannot verify browser generations before capture")
            pending = True
        else:
            generation_apps = apps if manual else sorted(BROWSER_CLASSES)
            for candidate in generation_apps:
                initial = identity_for_clients(clients, candidate)
                current = identity_for_clients(observed, candidate)
                if initial and current != initial:
                    print(f"[browser-repair] {candidate}: generation changed during repair")
                    pending = True
                    break
    if not persist_state(state_dir, state):
        return 3
    return 3 if pending else 0


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: browser_repair.py <session.toml> <state-dir> "
            "[--disabled|--dry-run|--manual] [--app CLASS] [--skip-app CLASS]",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--app", choices=sorted(BROWSER_CLASSES))
    parser.add_argument("--skip-app", action="append", choices=sorted(BROWSER_CLASSES), default=[])
    try:
        args = parser.parse_args(sys.argv[3:])
    except SystemExit:
        return 2
    try:
        clients = mapped_clients(json.loads(sys.stdin.read() or "[]"))
    except (json.JSONDecodeError, TypeError):
        print("[browser-repair] invalid clients JSON", file=sys.stderr)
        return 3
    return run(
        Path(sys.argv[1]),
        sys.argv[2],
        clients,
        disabled=args.disabled,
        dry_run=args.dry_run,
        manual=args.manual,
        app=args.app,
        skip_apps=set(args.skip_app),
    )


if __name__ == "__main__":
    raise SystemExit(main())
