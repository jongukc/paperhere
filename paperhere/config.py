import os
from pathlib import Path

TMP_PREFIX = "/tmp/paperhere-"
NVIM_PIPE_PREFIX = "/tmp/nvim-"
DEFAULT_TUNNEL_PORT = 12321
REMOTE_FORWARD_SCRIPT = Path.home() / "bin" / "paperhere-forward"

ZATHURARC_TEMPLATE = """\
set synctex true
set synctex-editor-command "{inverse_search_script} '%{{input}}' %{{line}}"
"""

INVERSE_SEARCH_LOCAL = """\
#!/bin/bash
INPUT="$1"
LINE="$2"
PROJECT_DIR="{project_dir}"
PROJECT_NAME="{project_name}"

# If file exists at synctex path, use it directly
if [ ! -f "$INPUT" ]; then
    # Extract relative path after project name and resolve in project dir
    REL="${{INPUT#*$PROJECT_NAME/}}"
    if [ "$REL" != "$INPUT" ]; then
        INPUT="$PROJECT_DIR/$REL"
    fi
fi

nvim --server {nvim_pipe} --remote-send "<C-\\\\><C-n>:e $INPUT<CR>:${{LINE}}<CR>"
"""

INVERSE_SEARCH_REMOTE = """\
#!/bin/bash
INPUT="$1"
LINE="$2"
LOCAL_MOUNT="{local_mount}"
REMOTE_DIR="{remote_dir}"
PROJECT_NAME="{project_name}"

# Try mount path substitution first
FILE="${{INPUT/$LOCAL_MOUNT/$REMOTE_DIR}}"

# If substitution didn't change anything, extract relative path after project name
if [ "$FILE" = "$INPUT" ]; then
    REL="${{INPUT#*$PROJECT_NAME/}}"
    if [ "$REL" != "$INPUT" ]; then
        FILE="$REMOTE_DIR/$REL"
    fi
fi

ssh {server} nvim --server {nvim_pipe} \
    --remote-send "'<C-\\\\><C-n>:e ${{FILE}}<CR>:${{LINE}}<CR>'"
"""

FORWARD_SCRIPT = """\
#!/bin/bash
echo "FORWARD $*" | nc -q0 localhost {port}
"""


def session_dir(project: str) -> Path:
    return Path(f"{TMP_PREFIX}{project}")


def nvim_pipe(project: str) -> str:
    return f"{NVIM_PIPE_PREFIX}{project}.pipe"
