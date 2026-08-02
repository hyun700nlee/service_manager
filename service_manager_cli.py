from __future__ import annotations

import argparse
import json

from ipc import EngineClient


def _print(response: dict) -> int:
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="servicemgr", description="Python Service Manager local CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    get_item = sub.add_parser("get")
    get_item.add_argument("kind", choices=("service", "remote_job"))
    get_item.add_argument("id")
    for command in ("start", "stop", "restart", "run"):
        child = sub.add_parser(command)
        child.add_argument("id")
    events = sub.add_parser("events")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--level")
    events.add_argument("--keyword")
    for command in ("import", "export"):
        child = sub.add_parser(command)
        child.add_argument("path")
    for command in ("create", "update", "test"):
        child = sub.add_parser(command)
        child.add_argument("kind", choices=("service", "remote_job"))
        child.add_argument("json_file")
        child.add_argument("--secret")
    delete = sub.add_parser("delete")
    delete.add_argument("kind", choices=("service", "remote_job"))
    delete.add_argument("id")
    diagnostics = sub.add_parser("diagnostics")
    diagnostics.add_argument("path", nargs="?")
    sub.add_parser("backup")
    args = parser.parse_args(argv)
    client = EngineClient()
    command = args.command
    if command == "status":
        response = client.request("list")
    elif command == "get":
        response = client.request("get", kind=args.kind, id=args.id)
    elif command in {"start", "stop", "restart", "run"}:
        response = client.request(command, id=args.id)
    elif command == "events":
        response = client.request("events", limit=args.limit, level=args.level, keyword=args.keyword)
    elif command in {"import", "export"}:
        response = client.request(command, path=args.path)
    elif command in {"create", "update", "test"}:
        with open(args.json_file, "r", encoding="utf-8") as file:
            item = json.load(file)
        response = client.request(command, kind=args.kind, item=item, secret=args.secret)
    elif command == "delete":
        response = client.request(command, kind=args.kind, id=args.id)
    elif command == "diagnostics":
        response = client.request(command, path=args.path)
    else:
        response = client.request("backup")
    return _print(response)


if __name__ == "__main__":
    raise SystemExit(main())
