#!/usr/bin/env python3
"""Prototype: restore one browser application's saved window placement.

This is deliberately outside the OmaSession UI and command dispatcher.  It
uses the same title matching and Hyprland move primitive as the live repair,
but takes an explicit saved session and requires ``--apply`` to move anything.
It never launches, closes, or edits the browser.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "lib"))

import browser_repair as repair  # noqa: E402
import replay as R  # noqa: E402
from safe_fs import Refused, _open_lock_fd, open_dir_chain  # noqa: E402


DEFAULT_SESSION = Path(
    os.environ.get(
        "OMASESSION_SESSION_DIR",
        str(Path.home() / ".local/share/omasession/sessions"),
    )
) / "last.toml"
ACTIVE_SESSION_DIR = DEFAULT_SESSION.parent


def hypr_json(command: str) -> object:
    try:
        out = subprocess.run(
            ["hyprctl", command, "-j"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"não foi possível ler hyprctl {command}: {exc}")


def clients() -> list[dict]:
    return repair.mapped_clients(hypr_json("clients"))


def monitor_labels() -> dict[str, str]:
    value = hypr_json("monitors")
    if not isinstance(value, list):
        return {}
    return {
        str(item.get("id")): str(item.get("name"))
        for item in value
        if item.get("name")
    }


def workspace_monitor(workspace_id: str) -> str | None:
    workspaces = hypr_json("workspaces")
    if isinstance(workspaces, list):
        for item in workspaces:
            if str(item.get("id", "")) == workspace_id:
                monitor = item.get("monitor")
                return str(monitor) if monitor else None
    # Empty workspaces are omitted. With exactly one enabled monitor the
    # association is unambiguous; with several monitors we refuse to guess.
    monitors = hypr_json("monitors")
    enabled = [
        item.get("name")
        for item in monitors
        if isinstance(item, dict) and not item.get("disabled")
    ]
    return str(enabled[0]) if len(enabled) == 1 else None


def load_reference(path: Path, app: str) -> list[dict]:
    if not path.is_file():
        raise RuntimeError(f"referência não encontrada: {path}")
    windows, titles, _ = R.load_pair(path)
    if not windows or not titles:
        raise RuntimeError("a referência não tem o par de títulos necessário")
    saved = [dict(item) for item in titles if item.get("class") == app]
    if not saved:
        raise RuntimeError(f"a referência não contém janelas da classe {app!r}")
    return saved


class Busy(RuntimeError):
    """The live snapshot/restore operation already owns the session lock."""


@contextmanager
def session_locks(path: Path):
    """Take the timer lock and the selected reference lock in stable order."""
    name = path.name.removesuffix(".toml")
    paths = {path.parent / ".last.lock", path.parent / f".{name}.lock"}
    handles = []
    try:
        for lock_path in sorted(paths, key=str):
            try:
                dir_fd = open_dir_chain(str(lock_path.parent), create=False)
                try:
                    handle = _open_lock_fd(dir_fd, lock_path.name, 0o600)
                finally:
                    os.close(dir_fd)
            except (FileNotFoundError, OSError, Refused) as exc:
                raise RuntimeError(f"não foi possível abrir o lock {lock_path.name}: {exc}")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(handle)
                raise Busy(f"operação concorrente em andamento ({lock_path.name})")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)


def title(window: dict) -> str:
    return str(window.get("title", ""))


def workspace(window: dict) -> str:
    return str(window.get("workspace", {}).get("id", ""))


def monitor_name(window: dict) -> str:
    if window.get("monitorName"):
        return str(window["monitorName"])
    if window.get("monitor") is not None:
        return str(window["monitor"])
    return "?"


def process_identity(window: dict) -> tuple[str, str, str] | None:
    try:
        pid = int(window.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    start = repair.proc_starttime(pid)
    if start is None:
        return None
    return str(pid), start, str(window.get("class", ""))


def same_window(before: dict, after: dict) -> bool:
    """Reject a stale plan before dispatching it to Hyprland."""
    before_identity = before.get("_identity")
    after_identity = process_identity(after)
    return (
        after.get("address") == before.get("address")
        and title(after) == title(before)
        and before_identity is not None
        and before_identity == after_identity
        and workspace(after) == workspace(before)
    )


def show_plan(current: list[dict], saved: list[dict]) -> list[tuple[dict, dict]]:
    matches = repair.match_plan(current, saved)
    if not matches:
        print("nenhuma janela do Chrome corresponde à referência")
        return []
    print(f"referência: {len(saved)} janela(s); correspondências: {len(matches)}")
    moves: list[tuple[dict, dict]] = []
    labels = monitor_labels()
    for window, target in matches:
        current_ws = workspace(window)
        target_ws = str(target.get("workspace", ""))
        current_mon = labels.get(str(window.get("monitor")), monitor_name(window))
        target_mon = monitor_name(target)
        monitor_ok = (
            not target.get("monitorName")
            or workspace_monitor(target_ws) == target.get("monitorName")
        )
        if current_ws == target_ws:
            state = "já está no destino" if monitor_ok else "workspace certo, monitor divergente"
        elif not monitor_ok:
            state = "monitor do destino não confirmado; será ignorada"
        else:
            state = "será movida"
            planned = dict(window)
            planned["_identity"] = process_identity(window)
            if planned["_identity"] is None:
                state = "sem identidade verificável; será ignorada"
            else:
                moves.append((planned, target))
        print(
            f"  {title(window)!r}: ws{current_ws}/{current_mon} → "
            f"ws{target_ws}/{target_mon} ({state})"
        )
    missing = len(saved) - len(matches)
    if missing:
        print(f"sem correspondência: {missing} janela(s); nenhuma será aberta")
    return moves


def apply_moves(moves: list[tuple[dict, dict]]) -> int:
    if not moves:
        print("nenhum movimento necessário")
        return 0
    applied = 0
    for window, target in moves:
        latest = clients()
        by_address = {item.get("address"): item for item in latest}
        current = by_address.get(window.get("address"))
        if not current or not same_window(window, current):
            print(
                f"plano expirou para {title(window)!r}; "
                f"movimentos enviados antes da falha: {applied}; confirmação pendente",
                file=sys.stderr,
            )
            return 3
        expected_monitor = target.get("monitorName")
        if expected_monitor and workspace_monitor(str(target.get("workspace", ""))) != expected_monitor:
            print(
                f"monitor do destino não corresponde para {title(window)!r}; "
                "movimento interrompido",
                file=sys.stderr,
            )
            return 3
        R.move_to(window["address"], target.get("workspace"))
        applied += 1
    verified = clients()
    by_address = {item.get("address"): item for item in verified}
    failed = []
    for window, target in moves:
        observed = by_address.get(window.get("address"), {})
        if (
            not observed
            or process_identity(observed) != window.get("_identity")
            or title(observed) != title(window)
            or workspace(observed) != str(target.get("workspace", ""))
            or (
                target.get("monitorName")
                and workspace_monitor(str(target.get("workspace", "")))
                != target.get("monitorName")
            )
        ):
            failed.append(title(window))
    if failed:
        print("movimentos não confirmados: " + ", ".join(repr(x) for x in failed), file=sys.stderr)
        return 3
    print(f"movidas e confirmadas: {len(moves)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="google-chrome", help="classe Hyprland (default: google-chrome)")
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--apply", action="store_true", help="executa os movimentos; sem isso, apenas simula")
    args = parser.parse_args()
    session = args.session.expanduser().absolute()
    active_dir = ACTIVE_SESSION_DIR.expanduser().absolute()
    if session.parent != active_dir:
        print(
            f"restore-app-test: --session precisa estar no diretório ativo {active_dir}",
            file=sys.stderr,
        )
        return 2

    try:
        # The timer and the normal restore use these same locks.  Keep the
        # reference read and the compositor operation in the same critical
        # section so a timer cannot publish a new layout between them.
        with session_locks(session):
            saved = load_reference(session, args.app)
            current = [item for item in clients() if item.get("class") == args.app]
            moves = show_plan(current, saved)
            if not args.apply:
                print("simulação apenas; use --apply para mover")
                return 0
            return apply_moves(moves)
    except Busy as exc:
        print(f"restore-app-test: {exc}", file=sys.stderr)
        return 4
    except RuntimeError as exc:
        print(f"restore-app-test: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
