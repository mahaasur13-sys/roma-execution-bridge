#!/usr/bin/env python3
"""A-0/P-LEASE-3: протокол аренды писателя — с маркерами сессий, которые реально обновляются.

Зачем (дефект P-3 из реестра аудитора): маркер `.sessions/<uuid>.json` писался один раз
при захвате и НЕ обновлялся вместе с heartbeat. Из-за этого `last_seen` «застывал»,
детектор «свежих маркеров больше одного» не мог сработать никогда, а живая сессия
выглядела мёртвой — инструмент, который не может сработать, выглядел работающим.

Правила:
  * любая запись — под `flock` и атомарно (`tmp` + `os.replace`);
  * `heartbeat` обновляет `heartbeat_at` в аренде И `last_seen`/`pid`/`boot_id` в маркере;
  * `fencing_token` увеличивается ТОЛЬКО при захвате (`capture`), не при heartbeat;
  * `guard` блокирует запись, если свежих маркеров (< ttl) больше одного, и требует эскалации;
  * P-LEASE-3: право перезахвата даёт ТОЛЬКО истёкший TTL, а не отсутствующий PID.
    `capture` под тем же `flock` и ДО любых изменений проверяет heartbeat/TTL и свежий чужой
    маркер: живая чужая аренда или неразбираемый heartbeat → exit 3, аренда/маркеры побайтово
    неизменны и новый маркер не создаётся; протухшая аренда → ровно один захват с token+1;
    держатель со своим `instance_id` продлевает СВОЮ аренду.

CLI:
    lease.py capture [instance_id]   # захват: token+1, новый/заданный instance_id, маркер
    lease.py heartbeat                # обновить аренду и СВОЙ маркер
    lease.py status [--json]          # состояние + строка fresh session markers: N
    lease.py guard                    # exit 0 если писать можно, exit 3 при коллизии
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import pathlib
import sys
import uuid

DEFAULT_ARTIFACTS_DIR = pathlib.Path(
    os.environ.get("ROMA_ARTIFACTS_DIR", "/home/workspace/artifacts")
)
TTL_MIN = 15
SCOPE = [
    "artifacts/**",
    "repo:roma-execution-bridge@branch:p1/pg-supervision-and-startup",
    "service:pg-watchdog",
]


class CollisionError(RuntimeError):
    """Писать нельзя: свежих маркеров больше одного, живая чужая аренда или повреждённое состояние."""


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(ts: datetime.datetime | None = None) -> str:
    return (ts or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def boot_id() -> str:
    try:
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def atomic_write(path: pathlib.Path, payload: dict) -> None:
    """Атомарная запись JSON: tmp в том же каталоге + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_state_strict(path: pathlib.Path) -> dict:
    """Строгое чтение аренды для решения о захвате.

    Отсутствие файла = `{}` (первый захват). Побитый/нечитаемый JSON — ОТКАЗ (fail-closed):
    иначе повреждённая аренда выглядела бы как «свободно», и перо забиралось бы молча.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollisionError(
            f"CAPTURE REFUSED: {path} не читается/не разбирается ({exc.__class__.__name__}) — "
            "состояние аренды повреждено (fail-closed); token/аренда/маркеры не изменены."
        ) from exc
    if not isinstance(data, dict):
        raise CollisionError(
            f"CAPTURE REFUSED: {path} — не объект JSON (fail-closed); token/аренда/маркеры не изменены."
        )
    return data


def parse_heartbeat_at(value) -> datetime.datetime:
    """Строгий разбор `heartbeat_at`.

    Пустое/неразбираемое значение — ОТКАЗ, а не «протухшая аренда»: право перезахвата
    определяется только разбираемым heartbeat, иначе повреждённое поле молча открыло бы перо.
    """
    if not isinstance(value, str) or not value.strip():
        raise CollisionError(
            f"CAPTURE REFUSED: heartbeat_at={value!r} пуст или не строка — аренда повреждена "
            "(fail-closed); token/аренда/маркеры не изменены."
        )
    try:
        ts = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollisionError(
            f"CAPTURE REFUSED: heartbeat_at={value!r} не разбирается ({exc.__class__.__name__}) — "
            "аренда повреждена (fail-closed); token/аренда/маркеры не изменены."
        ) from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def marker_path(sessions_dir: pathlib.Path, instance_id: str) -> pathlib.Path:
    return sessions_dir / f"{instance_id}.json"


def write_marker(
    sessions_dir: pathlib.Path, instance_id: str, *, conversation_id: str = ""
) -> pathlib.Path:
    """Создать/обновить маркер сессии: last_seen, pid, boot_id — всегда свежие."""
    path = marker_path(sessions_dir, instance_id)
    previous = read_json(path)
    payload = {
        "instance_id": instance_id,
        "conversation_id": conversation_id or previous.get("conversation_id", ""),
        "first_seen": previous.get("first_seen") or iso(),
        "last_seen": iso(),
        "pid": os.getpid(),
        "host": os.uname().nodename,
        "boot_id": boot_id(),
    }
    atomic_write(path, payload)
    return path


def fresh_markers(
    sessions_dir: pathlib.Path, ttl_min: int = TTL_MIN
) -> dict[str, dict]:
    """Маркеры, чей last_seen моложе ttl. Живая сессия обязана обновлять last_seen."""
    out: dict[str, dict] = {}
    if not sessions_dir.is_dir():
        return out
    now = utcnow()
    for path in sorted(sessions_dir.glob("*.json")):
        data = read_json(path)
        if not data:
            continue
        try:
            seen = datetime.datetime.fromisoformat(
                str(data["last_seen"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError):
            continue
        if (now - seen).total_seconds() < ttl_min * 60:
            out[data.get("instance_id", path.stem)] = data
    return out


def guard(
    sessions_dir: pathlib.Path,
    ttl_min: int = TTL_MIN,
    *,
    own_instance: str | None = None,
) -> dict[str, dict]:
    """Проверка права записи. Возвращает свежие маркеры или бросает CollisionError.

    Своим маркером считается только `own_instance` — если он передан. Иначе «своим»
    считается единственный свежий маркер (совместимость с ручным запуском).
    """
    fresh = fresh_markers(sessions_dir, ttl_min)
    if own_instance:
        others = {k: v for k, v in fresh.items() if k != own_instance}
        if others:
            raise CollisionError(_collision_message(fresh))
        return fresh
    if len(fresh) > 1:
        raise CollisionError(_collision_message(fresh))
    return fresh


def _collision_message(fresh: dict[str, dict]) -> str:
    rows = "\n".join(
        f"  - {iid} pid={d.get('pid')} last_seen={d.get('last_seen')}"
        for iid, d in fresh.items()
    )
    return (
        "LEASE COLLISION: свежих маркеров сессий больше одного (< ttl). "
        "Писать нельзя никому — эскалация владельцу.\n" + rows
    )


class Lease:
    """Аренда писателя: lease-файл + маркер сессии + lock-файл."""

    def __init__(
        self,
        artifacts_dir: pathlib.Path = DEFAULT_ARTIFACTS_DIR,
        ttl_min: int = TTL_MIN,
    ):
        self.dir = pathlib.Path(artifacts_dir)
        self.sessions = self.dir / ".sessions"
        self.lease_path = self.dir / ".writer_lease.json"
        self.lock_path = self.dir / ".writer_lease.lock"
        self.ttl_min = ttl_min
        # явные области измерения последней попытки захвата (печатаются в CLI, «ничего неявного»)
        self.capture_checks: dict = {}

    @property
    def state(self) -> dict:
        return read_json(self.lease_path)

    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _assert_capture_allowed(
        self, state: dict, requested: str | None, iid: str
    ) -> dict:
        """P-LEASE-3: проверки права захвата ДО любых изменений (под тем же flock).

        Возвращает явный отчёт о применённых проверках; при отказе бросает CollisionError
        (CLI exit 3), не создавая маркер и не переписывая аренду.
        """
        ttl = self.ttl_min
        checks: dict = {
            "ttl_min": ttl,
            "holder": state.get("instance_id"),
            "requested_instance": requested or f"(new {iid})",
        }
        if not state:
            checks.update(
                {
                    "lease": "absent",
                    "heartbeat_age_min": None,
                    "fresh_foreign_markers": 0,
                }
            )
            self.capture_checks = checks
            return checks

        hb = state.get("heartbeat_at")
        age_min = round((utcnow() - parse_heartbeat_at(hb)).total_seconds() / 60, 1)
        checks.update({"heartbeat_at": hb, "heartbeat_age_min": age_min})
        holder = state.get("instance_id")
        foreign = {
            iid_: data
            for iid_, data in fresh_markers(self.sessions, ttl).items()
            if iid_ not in {holder, iid}
        }
        checks["fresh_foreign_markers"] = len(foreign)

        if age_min <= ttl:
            if not (requested and requested == holder):
                raise CollisionError(
                    "CAPTURE REFUSED: аренда жива — heartbeat_age=%.1fmin <= ttl=%dmin, holder=%s, "
                    "requested=%s. Право перезахвата даёт ТОЛЬКО истёкший TTL (или явный stand-down "
                    "владельца); token/аренда/маркеры не изменены."
                    % (age_min, ttl, holder, requested or f"(new {iid})")
                )
            if foreign:
                raise CollisionError(_collision_message({holder: state, **foreign}))
            checks["lease"] = f"live-own(holder={holder})"
            self.capture_checks = checks
            return checks

        checks["lease"] = f"stale(heartbeat_age={age_min}min > ttl={ttl}min)"
        if foreign:
            raise CollisionError(
                "CAPTURE REFUSED: аренда протухла, но свежие чужие маркеры сессий живы: %s. "
                "Писать нельзя никому — эскалация владельцу; token/аренда/маркеры не изменены."
                % ", ".join(sorted(foreign))
            )
        self.capture_checks = checks
        return checks

    def capture(
        self, instance_id: str | None = None, *, task: str = "", status: str = ""
    ) -> dict:
        iid = instance_id or str(uuid.uuid4())
        with self._locked():
            old = read_state_strict(self.lease_path)
            self._assert_capture_allowed(old, instance_id, iid)
            payload = {
                "conversation_id": old.get("conversation_id", ""),
                "instance_id": iid,
                "fencing_token": int(old.get("fencing_token") or 0) + 1,
                "boot_id": boot_id(),
                "owner": old.get("owner", ""),
                "pid": os.getpid(),
                "host": os.uname().nodename,
                "started_at": iso(),
                "heartbeat_at": iso(),
                "ttl_min": self.ttl_min,
                "scope": old.get("scope", SCOPE),
                "task": task or old.get("task", ""),
                "status": status or old.get("status", ""),
            }
            for keep in ("released_at", "released_by", "previous_status"):
                if keep in old:
                    payload.setdefault(keep, old[keep])
            atomic_write(self.lease_path, payload)
            write_marker(
                self.sessions, iid, conversation_id=payload.get("conversation_id", "")
            )
            return payload

    def heartbeat(self) -> dict:
        """Обновить аренду И маркер: last_seen сессии обязан жить вместе с heartbeat (A-0)."""
        with self._locked():
            state = self.state
            iid = state.get("instance_id")
            if not iid:
                raise CollisionError(
                    "heartbeat без захваченной аренды: сначала `lease.py capture`"
                )
            guard(self.sessions, self.ttl_min, own_instance=iid)
            state["heartbeat_at"] = iso()
            state["pid"] = os.getpid()
            state["boot_id"] = boot_id()
            atomic_write(self.lease_path, state)
            write_marker(
                self.sessions, iid, conversation_id=state.get("conversation_id", "")
            )
            return state

    def status(self) -> dict:
        state = self.state
        fresh = fresh_markers(self.sessions, self.ttl_min)
        hb = state.get("heartbeat_at")
        age_min = None
        if hb:
            try:
                age_min = round(
                    (
                        utcnow()
                        - datetime.datetime.fromisoformat(hb.replace("Z", "+00:00"))
                    ).total_seconds()
                    / 60,
                    1,
                )
            except ValueError:
                age_min = None
        return {
            "instance_id": state.get("instance_id"),
            "fencing_token": state.get("fencing_token"),
            "heartbeat_at": hb,
            "heartbeat_age_min": age_min,
            "lease_live": bool(age_min is not None and age_min <= self.ttl_min),
            "fresh_session_markers": len(fresh),
            "fresh_markers": sorted(fresh),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Lease v2 (A-0/P-LEASE-3): аренда писателя + маркеры сессий"
    )
    ap.add_argument("command", choices=("capture", "heartbeat", "status", "guard"))
    ap.add_argument("instance_id", nargs="?", default=None)
    ap.add_argument("--artifacts-dir", default=str(DEFAULT_ARTIFACTS_DIR))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    lease = Lease(pathlib.Path(args.artifacts_dir))
    try:
        if args.command == "capture":
            state = lease.capture(args.instance_id)
            checks = lease.capture_checks
            print(
                f"LEASED instance_id={state['instance_id']} fencing_token={state['fencing_token']}"
            )
            print(
                "checks: lease={lease} · heartbeat_age_min={heartbeat_age_min} · ttl_min={ttl_min} "
                "· requested={requested_instance} · fresh_foreign_markers={fresh_foreign_markers}".format(
                    **{
                        "lease": checks.get("lease"),
                        "heartbeat_age_min": checks.get("heartbeat_age_min"),
                        "ttl_min": checks.get("ttl_min"),
                        "requested_instance": checks.get("requested_instance"),
                        "fresh_foreign_markers": checks.get("fresh_foreign_markers"),
                    }
                )
            )
            print(f"marker: {lease.sessions / (state['instance_id'] + '.json')}")
            return 0
        if args.command == "heartbeat":
            state = lease.heartbeat()
            st = lease.status()
            print(
                f"HEARTBEAT {state['heartbeat_at']} · instance_id={state['instance_id']} "
                f"· fencing_token={state['fencing_token']}"
            )
            print(f"fresh session markers: {st['fresh_session_markers']}")
            return 0
        if args.command == "status":
            st = lease.status()
            if args.json:
                print(json.dumps(st, ensure_ascii=False))
            else:
                for key, value in st.items():
                    print(f"{key:>22}: {value}")
                print(f"fresh session markers: {st['fresh_session_markers']}")
            return 0
        guard(
            lease.sessions, lease.ttl_min, own_instance=lease.state.get("instance_id")
        )
        print(
            f"GUARD OK: fresh session markers: {len(fresh_markers(lease.sessions, lease.ttl_min))}"
        )
        return 0
    except CollisionError as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
