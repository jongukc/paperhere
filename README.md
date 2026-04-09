# paperhere

LaTeX editing with bidirectional SyncTeX: nvim on local or remote servers, Zathura PDF viewer locally.

Automates sshfs mounting, zathura config, reverse SSH tunnels, and path translation into a single command.

## Install

```bash
git clone git@github.com:jongukc/paperhere.git
pip install -e ./paperhere
```

## Usage

### Local mode

Edit LaTeX locally with synctex between nvim and zathura.

```bash
paperhere local <project-dir> [--pdf <name>] [--build-cmd '<cmd>']
```

Launches zathura in the background and starts nvim with a named pipe. Forward search (`\lv` in vimtex) jumps to the PDF location; ctrl+click in zathura jumps back to the source.

With `--build-cmd`, the project is automatically rebuilt whenever a `.tex` file is saved:

```bash
paperhere local ./myproject --build-cmd 'make -C paper'
```

### Remote mode

Edit LaTeX on a remote server with the PDF viewer running locally.

```bash
paperhere remote <server> <remote-dir> [--pdf <name>] [--port <N>] [--build-cmd '<cmd>']
```

This:

1. Mounts the remote directory via sshfs
2. Opens zathura locally with the mounted PDF
3. Starts a TCP listener for forward search commands
4. Opens a reverse SSH tunnel so the remote can reach the listener
5. Deploys `~/bin/paperhere-forward` to the remote server

Then SSH into the server and start nvim:

```bash
PAPERHERE_SESSION=1 PAPERHERE_BUILD_CMD='make -C paper' nvim --listen /tmp/nvim-<project>.pipe .
```

The `PAPERHERE_BUILD_CMD` env var is optional; when set, `.tex` file saves trigger the build automatically.

Forward search (nvim -> PDF) goes through the TCP tunnel. Inverse search (PDF -> nvim) goes through SSH.

### Stop

```bash
paperhere stop [project]    # stop one or all sessions
```

Kills zathura, tears down the SSH tunnel, unmounts sshfs, and removes temp files.

## Requirements

- Python >= 3.10 (stdlib only, no dependencies)
- [nvim](https://neovim.io/) with the [vimtex](https://github.com/lervag/vimtex) plugin
- [zathura](https://pwmt.org/projects/zathura/) (with synctex support)
- sshfs and fusermount (remote mode)
- ssh (remote mode)
- netcat (`nc`) on the remote server (remote mode)

## How it works

```
Local machine                          Remote server
-------------------------------------------------
zathura <-- sshfs mount                nvim --listen pipe
  |                                      |
  |--- inverse search (PDF click) ------>| (via ssh)
  |                                      |
  |<-- forward search (\lv) -------------| (paperhere-forward -> nc -> TCP tunnel)
```

The TCP listener on the local machine receives forward search commands over a reverse SSH tunnel. It translates remote paths to local mount paths and calls `zathura --synctex-forward`.

Inverse search uses zathura's `synctex-editor-command`, which runs an SSH command to send `--remote-send` to the remote nvim pipe.

## VimTeX integration

The vimtex plugin config below detects `PAPERHERE_SESSION` env var.
When set, it routes forward search through `~/bin/paperhere-forward` (deployed automatically)
instead of calling zathura directly.

```lua
return {
    "lervag/vimtex",
    lazy = false,
    init = function()
        vim.g.vimtex_syntax_enabled = 1

        -- Use paperhere-forward for remote sessions, zathura directly for local
        local forward = os.getenv("HOME") .. "/bin/paperhere-forward"
        if vim.fn.filereadable(forward) == 1
           and vim.fn.getenv("PAPERHERE_SESSION") ~= vim.NIL then
            vim.g.vimtex_view_method = "general"
            vim.g.vimtex_view_general_viewer = forward
            vim.g.vimtex_view_general_options = "@line:@col:@tex @pdf"
        else
            vim.g.vimtex_view_method = "zathura"
        end

        -- Auto-rebuild on save when PAPERHERE_BUILD_CMD is set
        local build_cmd = vim.fn.getenv("PAPERHERE_BUILD_CMD")
        if build_cmd ~= vim.NIL then
            vim.api.nvim_create_autocmd("BufWritePost", {
                pattern = "*.tex",
                callback = function()
                    vim.fn.jobstart(build_cmd)
                end,
            })
        end
    end,
}
```
