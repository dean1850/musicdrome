"""Command-line utilities: ``python -m musicdrome.cli <command>``."""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from . import __version__
from .auth import create_user, set_password
from .config import settings
from .db import init_db, session_scope
from .main import configure_logging
from .models import User


def cmd_scan(args: argparse.Namespace) -> int:
    from .services import scanner

    init_db()
    result = scanner.scan_library(full=args.full)
    print(
        f"scanned {result.seen} files: {result.added} added, "
        f"{result.updated} updated, {result.removed} removed, {result.errors} errors"
    )
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    from .services.smartplaylist import seed_default_playlists

    init_db()
    password = args.password or getpass.getpass("Password: ")
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1

    with session_scope() as db:
        if db.scalar(select(User).where(User.username == args.username)):
            print(f"user '{args.username}' already exists", file=sys.stderr)
            return 1
        user = create_user(db, args.username, password, is_admin=args.admin)
        seed_default_playlists(db, user)

    print(f"created {'admin' if args.admin else 'user'} '{args.username}'")
    return 0


def cmd_set_password(args: argparse.Namespace) -> int:
    init_db()
    password = args.password or getpass.getpass("New password: ")
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1

    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1
        set_password(db, user, password)

    print(f"password updated for '{args.username}'")
    return 0


def cmd_list_users(_args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as db:
        users = db.scalars(select(User).order_by(User.username)).all()
        if not users:
            print("no users yet")
            return 0
        width = max(len(u.username) for u in users)
        for user in users:
            flags = []
            if user.is_admin:
                flags.append("admin")
            if not user.is_active:
                flags.append("disabled")
            print(f"{user.username:<{width}}  {' '.join(flags) or 'user'}")
    return 0


def cmd_config(_args: argparse.Namespace) -> int:
    print(f"Musicdrome {__version__}")
    print(f"  music dir     {settings.music_dir}")
    print(f"  data dir      {settings.data_dir}")
    print(f"  cache dir     {settings.cache_dir}")
    print(f"  database      {settings.database_url}")
    print(f"  AI provider   {settings.ai_provider} ({settings.anthropic_model})")
    print(f"  transcoding   {'on' if settings.transcoding_enabled else 'off'}")
    print(f"  lidarr        {'on' if settings.lidarr_enabled else 'off'}")
    print(f"  acquisition   {'on' if settings.acquisition_enabled else 'off'}")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    init_db()
    if args.what == "playlists":
        from .services.smartplaylist import refresh_all

        print(refresh_all())
    elif args.what == "recommendations":
        from .services.recommendations import refresh_all

        print(refresh_all())
    elif args.what == "podcasts":
        from .services.podcasts import refresh_all

        print(refresh_all())
    elif args.what == "metadata":
        from .services.enrich import enrich_library

        print(enrich_library(limit=200))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musicdrome", description="Musicdrome CLI")
    parser.add_argument("--version", action="version", version=f"musicdrome {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan the music library")
    scan.add_argument("--full", action="store_true", help="re-read tags for every file")
    scan.set_defaults(func=cmd_scan)

    create = sub.add_parser("create-user", help="create a user account")
    create.add_argument("username")
    create.add_argument("--password", help="read from a prompt when omitted")
    create.add_argument("--admin", action="store_true")
    create.set_defaults(func=cmd_create_user)

    passwd = sub.add_parser("set-password", help="change a user's password")
    passwd.add_argument("username")
    passwd.add_argument("--password")
    passwd.set_defaults(func=cmd_set_password)

    users = sub.add_parser("list-users", help="list accounts")
    users.set_defaults(func=cmd_list_users)

    config = sub.add_parser("config", help="show the effective configuration")
    config.set_defaults(func=cmd_config)

    refresh = sub.add_parser("refresh", help="run a maintenance task now")
    refresh.add_argument(
        "what", choices=["playlists", "recommendations", "podcasts", "metadata"]
    )
    refresh.set_defaults(func=cmd_refresh)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
