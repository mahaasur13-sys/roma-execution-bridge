"""Внешняя точка входа адаптера: state-файл на входе, вердикт-файл на выходе.

Запуск вне ядра ROMA:

    python -m adapters.jev.run --state-file state.json
    python -m adapters.jev.run --state-file state.json --out decision.json
    python -m adapters.jev.run --state-file state.json --model jev-latest   # ручной smoke

Коды выхода: `0` — есть решение (`execute`/`confirm`/`abort`); `4` — `skip`
(вердикт получен, решения нет: вызывающему нельзя принять это за согласие);
`2` — ошибка использования; `3` — файл state не прочитан; `5` — вердикт получен,
но результат не записан (ошибка вывода: целевой файл не тронут, вердикт не
переклассифицирован и провайдер повторно не вызывается).
Секреты не печатаются: state перед отправкой редактируется, ключ не логируется.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from adapters.jev.adapter import JevAdapter
from adapters.jev.client import JevClient, mask
from adapters.jev.contract import DEFAULT_MODEL, SMOKE_MODEL

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STATE_UNREADABLE = 3
EXIT_SKIP = 4
EXIT_OUTPUT_ERROR = 5


def parse_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """Разбирает argv, оставляя код использования выраженным через `EXIT_USAGE`.

    argparse сам бросает `SystemExit(2)`; здесь он перевыбрасывается тем же кодом,
    чтобы константа не была мёртвым числом. `--help` (`SystemExit(0)`) и любой
    другой статус уходят наружу без изменений.
    """
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == EXIT_USAGE:
            raise SystemExit(EXIT_USAGE) from None
        raise


def _write_atomic(path: Path, text: str) -> None:
    """Пишет результат атомарно: временный файл рядом + `os.replace`.

    Ошибка записи не должна оставлять целевой файл частично записанным: при
    сбое временный файл удаляется, а прежнее содержимое цели остаётся как было.
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adapters.jev.run",
        description="Sidecar-адаптер Jev: state + закрытые вопросы -> execute/confirm/abort/skip",
    )
    parser.add_argument("--state-file", required=True, help="JSON-файл со state задачи")
    parser.add_argument("--out", default="", help="файл для вердикта (по умолчанию — stdout)")
    parser.add_argument("--domain", default="", help="явный домен операции (иначе берётся поле act)")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"модель провайдера; {SMOKE_MODEL} — только ручной smoke, не регулярный путь",
    )
    parser.add_argument("--timeout", type=float, default=None, help="таймаут одного вызова, секунды")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parse_args(parser, argv)
    if args.model == SMOKE_MODEL:
        print(f"[jev] ручной smoke на {SMOKE_MODEL}: не для регулярных прогонов", file=sys.stderr)
    path = Path(args.state_file)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[jev] state не прочитан: {exc}", file=sys.stderr)
        return EXIT_STATE_UNREADABLE

    client = JevClient(model=args.model, timeout=args.timeout) if args.timeout else JevClient(model=args.model)
    verdict = JevAdapter(client=client).decide(raw, domain=args.domain or None)
    text = json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        try:
            _write_atomic(Path(args.out), text + "\n")
        except OSError as exc:
            print(f"[jev] результат не записан: {mask(exc, client.api_key)}", file=sys.stderr)
            return EXIT_OUTPUT_ERROR
    else:
        print(text)
    return EXIT_OK if verdict.get("decision") != "skip" else EXIT_SKIP


if __name__ == "__main__":
    raise SystemExit(main())
