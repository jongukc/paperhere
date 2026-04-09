import subprocess
import sys
from pathlib import Path

from .config import session_dir, nvim_pipe, ZATHURARC_TEMPLATE, INVERSE_SEARCH_LOCAL
from .session import Session
from .cleanup import install_signal_handlers


def find_pdf(project_dir: Path, pdf_arg: str | None) -> Path:
    if pdf_arg:
        pdf = project_dir / pdf_arg
        if not pdf.exists():
            print(f"Error: PDF not found: {pdf}", file=sys.stderr)
            sys.exit(1)
        return pdf
    pdfs = list(project_dir.glob("*.pdf"))
    if not pdfs:
        # check common subdirs
        for subdir in ("build", "output", "out"):
            pdfs = list((project_dir / subdir).glob("*.pdf"))
            if pdfs:
                break
    if not pdfs:
        print("Error: No PDF found in project directory. Use --pdf to specify.", file=sys.stderr)
        sys.exit(1)
    if len(pdfs) > 1:
        print(f"Multiple PDFs found, using: {pdfs[0].name} (use --pdf to override)", file=sys.stderr)
    return pdfs[0]


def run_local(args) -> None:
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"Error: Not a directory: {project_dir}", file=sys.stderr)
        sys.exit(1)

    project = project_dir.name
    sdir = session_dir(project)
    sdir.mkdir(parents=True, exist_ok=True)

    pdf = find_pdf(project_dir, args.pdf)
    pipe = nvim_pipe(project)

    # Generate inverse-search script
    inverse_script = sdir / "inverse-search.sh"
    inverse_script.write_text(INVERSE_SEARCH_LOCAL.format(
        project_dir=str(project_dir),
        project_name=project,
        nvim_pipe=pipe,
    ))
    inverse_script.chmod(0o755)

    # Write zathurarc
    zathurarc = sdir / "zathurarc"
    zathurarc.write_text(ZATHURARC_TEMPLATE.format(
        inverse_search_script=str(inverse_script),
    ))

    # Create session
    session = Session(
        project=project,
        mode="local",
        project_dir=str(project_dir),
    )

    # Launch zathura as a background process
    zathura_proc = subprocess.Popen(
        ["zathura", "-c", str(sdir), str(pdf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session.zathura_pid = zathura_proc.pid
    session.save()

    install_signal_handlers(session)

    # Replace current process with nvim
    import os
    if args.build_cmd:
        os.environ["PAPERHERE_BUILD_CMD"] = args.build_cmd
    os.execvp("nvim", ["nvim", "--listen", pipe, str(project_dir)])
