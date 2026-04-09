import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="paperhere",
        description="LaTeX editing with bidirectional synctex support on nvim and zathura",
    )
    sub = parser.add_subparsers(dest="command")

    # local
    p_local = sub.add_parser("local", help="Local LaTeX editing with zathura + nvim")
    p_local.add_argument("project_dir", help="Path to LaTeX project directory")
    p_local.add_argument("--pdf", help="PDF filename (auto-detected if omitted)")
    p_local.add_argument("--build-cmd", help="Build command to run on save (e.g. 'make -C paper')")

    # remote
    p_remote = sub.add_parser("remote", help="Remote LaTeX editing via sshfs + tunnel")
    p_remote.add_argument("server", help="SSH server (e.g. user@host)")
    p_remote.add_argument("remote_dir", help="Remote project directory path")
    p_remote.add_argument("--pdf", help="PDF filename (auto-detected if omitted)")
    p_remote.add_argument("--port", type=int, default=None, help="Tunnel port (default: 12321)")
    p_remote.add_argument("--build-cmd", help="Build command to run on save (e.g. 'make -C paper')")

    # stop
    p_stop = sub.add_parser("stop", help="Stop a paperhere session")
    p_stop.add_argument("project", nargs="?", help="Project name (stops all if omitted)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "local":
        from .local import run_local
        run_local(args)
    elif args.command == "remote":
        from .remote import run_remote
        run_remote(args)
    elif args.command == "stop":
        from .cleanup import teardown
        from .session import Session
        if args.project:
            session = Session.find(args.project)
            if session is None:
                print(f"No session found for project: {args.project}", file=sys.stderr)
                sys.exit(1)
            teardown(session)
        else:
            sessions = Session.find_all()
            if not sessions:
                print("No active sessions found.")
            for s in sessions:
                teardown(s)


if __name__ == "__main__":
    main()
