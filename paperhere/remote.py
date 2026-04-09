import atexit
import subprocess
import sys
import time
from pathlib import Path

from .config import (
    session_dir, nvim_pipe, DEFAULT_TUNNEL_PORT,
    ZATHURARC_TEMPLATE, INVERSE_SEARCH_REMOTE, FORWARD_SCRIPT,
)
from .session import Session
from .tunnel import ForwardSearchListener, check_port_available, start_reverse_tunnel
from .cleanup import teardown, install_signal_handlers


def find_pdf_in_mount(mount_path: Path, pdf_arg: str | None) -> Path:
    if pdf_arg:
        pdf = mount_path / pdf_arg
        if not pdf.exists():
            print(f"Error: PDF not found: {pdf}", file=sys.stderr)
            sys.exit(1)
        return pdf
    pdfs = list(mount_path.glob("*.pdf"))
    if not pdfs:
        for subdir in ("build", "output", "out"):
            pdfs = list((mount_path / subdir).glob("*.pdf"))
            if pdfs:
                break
    if not pdfs:
        print("Error: No PDF found in mounted directory. Use --pdf to specify.", file=sys.stderr)
        sys.exit(1)
    if len(pdfs) > 1:
        print(f"Multiple PDFs found, using: {pdfs[0].name} (use --pdf to override)", file=sys.stderr)
    return pdfs[0]


def run_remote(args) -> None:
    server = args.server
    remote_dir = args.remote_dir.rstrip("/")
    project = Path(remote_dir).name
    port = args.port or DEFAULT_TUNNEL_PORT

    sdir = session_dir(project)
    sdir.mkdir(parents=True, exist_ok=True)
    mount_path = sdir / "mount"
    mount_path.mkdir(exist_ok=True)

    # Check port availability
    if not check_port_available(port):
        print(f"Error: Port {port} is already in use. Use --port to specify another.", file=sys.stderr)
        sys.exit(1)

    # sshfs mount
    print(f"Mounting {server}:{remote_dir} ...")
    result = subprocess.run(
        ["sshfs", f"{server}:{remote_dir}", str(mount_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: sshfs mount failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Create session and register cleanup immediately after mount
    # so any subsequent error (e.g. PDF not found) still unmounts
    pipe = nvim_pipe(project)
    session = Session(
        project=project,
        mode="remote",
        mount_path=str(mount_path),
        server=server,
        remote_dir=remote_dir,
    )
    atexit.register(teardown, session)
    install_signal_handlers(session)

    # Find PDF
    pdf = find_pdf_in_mount(mount_path, args.pdf)

    # Generate inverse-search script
    inverse_script = sdir / "inverse-search.sh"
    inverse_script.write_text(INVERSE_SEARCH_REMOTE.format(
        local_mount=str(mount_path),
        remote_dir=remote_dir,
        project_name=project,
        server=server,
        nvim_pipe=pipe,
    ))
    inverse_script.chmod(0o755)

    # Write zathurarc
    zathurarc = sdir / "zathurarc"
    zathurarc.write_text(ZATHURARC_TEMPLATE.format(
        inverse_search_script=str(inverse_script),
    ))

    # Launch zathura as a background process
    zathura_proc = subprocess.Popen(
        ["zathura", "-c", str(sdir), str(pdf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session.zathura_pid = zathura_proc.pid

    # Start TCP listener
    listener = ForwardSearchListener(
        port=port,
        zathura_pid=session.zathura_pid,
        remote_dir=remote_dir,
        local_mount=str(mount_path),
    )
    listener.start()
    session.listener_port = port

    # Start reverse SSH tunnel
    tunnel_proc = start_reverse_tunnel(server, port)
    session.tunnel_pid = tunnel_proc.pid

    # Deploy paperhere-forward script to remote
    forward_content = FORWARD_SCRIPT.format(port=port)
    deploy_cmd = f"mkdir -p ~/bin && cat > ~/bin/paperhere-forward && chmod +x ~/bin/paperhere-forward"
    subprocess.run(
        ["ssh", server, deploy_cmd],
        input=forward_content, text=True,
        capture_output=True,
    )

    session.save()

    build_env = ""
    if args.build_cmd:
        build_env = f" PAPERHERE_BUILD_CMD='{args.build_cmd}'"

    print(f"""
paperhere session started: {project}
  Server:  {server}:{remote_dir}
  Mounted: {mount_path}
  Port:    {port}

On the remote, start nvim:
  PAPERHERE_SESSION=1{build_env} nvim --listen {pipe} .
""")

    # Keep the process alive (listener runs in background thread)
    try:
        while True:
            # Check if tunnel is still alive
            if tunnel_proc.poll() is not None:
                print("SSH tunnel died, restarting...", file=sys.stderr)
                tunnel_proc = start_reverse_tunnel(server, port)
                session.tunnel_pid = tunnel_proc.pid
                session.save()
            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        listener.stop()
