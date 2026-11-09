# Paperhere

Paperhere gives terminal Neovim a VS Code-like LaTeX loop: automatic builds, a live GUI preview, and bidirectional SyncTeX—locally or through SSH—with one command.

The preview is a small, local-only web application powered by a vendored PDF.js build. It refreshes when the PDF changes while preserving the current page and scroll position, and it supports Vim-style navigation. Remote projects stay remote: Paperhere deploys a cached agent, runs Neovim and SyncTeX on the server, and exposes the preview through an SSH local-forward. It does not use SSHFS or translate mounted paths.

## Install

```bash
git clone git@github.com:jongukc/paperhere.git
python -m pip install -e ./paperhere
```

Paperhere uses your existing Neovim configuration. VimTeX must be installed and available in that configuration; no permanent Paperhere-specific Neovim settings or environment variables are required.

## Use

Open a local project:

```bash
paperhere open ~/code/paper --build-cmd make
```

Open a project over SSH:

```bash
paperhere open server:~/code/paper --build-cmd make
```

Paperhere detects a top-level TeX root and uses the PDF with the same basename. Override either when a project has a different layout:

```bash
paperhere open ./paper --tex manuscript/main.tex --pdf build/main.pdf
```

Choose a specific local browser with `--browser`:

```bash
paperhere open ./paper --browser firefox
```

This also applies to SSH projects: the selected browser runs locally, while the editor and preview agent remain on the remote host.

When `--build-cmd` is supplied, Paperhere runs it at startup and after saves to `.tex`, `.bib`, `.sty`, or `.cls` files. Builds are serialized, so a save during compilation schedules one follow-up build instead of starting competing processes.

Without `--build-cmd`, Paperhere starts VimTeX's continuous compiler (`:VimtexCompile!`), which normally uses latexmk. Use `--no-auto-build` to leave compilation entirely under your control. The PDF watcher still refreshes the browser whenever another tool updates the file.

Useful options:

```text
--tex PATH          main TeX file
--pdf PATH          PDF output
--build-cmd CMD     custom build command
--nvim PATH         Neovim executable (also supported for SSH targets)
--port PORT         fixed local preview port
--browser NAME      browser to open, for example firefox
--no-auto-build     do not start a compiler
--no-browser        print the preview URL without opening it
```

## SyncTeX

- Neovim to PDF: run `:VimtexView` (usually `<localleader>lv`). The browser jumps to and highlights the matching PDF position.
- PDF to Neovim: Ctrl-click the rendered page. Neovim opens the matching source file and moves to the resolved line.

The build must produce a `.synctex.gz` file next to the PDF. With latexmk this generally means enabling `-synctex=1`; custom build systems must pass the equivalent engine option.

## Browser navigation

The viewer keeps focus-friendly Zathura-style controls:

| Key                  | Action                                      |
| -------------------- | ------------------------------------------- |
| `j` / `k`, `h` / `l` | Scroll vertically or horizontally           |
| `J` / `K`            | Next or previous PDF page                   |
| `gg` / `G`           | First or last page                          |
| `Ctrl-d` / `Ctrl-u`  | Half-page down or up                        |
| `Ctrl-f` / `Ctrl-b`  | Full-page down or up                        |
| `+` / `-`            | Zoom in or out                              |
| `s` / `a`            | Fit width or fit page                       |
| `/`, `n` / `N`       | Search text and move through highlighted matches |
| `r`                  | Reload the PDF                              |
| `?` / `Escape`       | Open help, close an overlay, or clear search |
| Click a link         | Follow a reference, citation, or URL        |
| `Ctrl-o` / `Ctrl-i`  | Jump back or forward after following a link |
| Ctrl-click           | Inverse SyncTeX                             |

Links come from the PDF itself: load `hyperref` so references, citations, and URLs become clickable. Internal links jump to the target; URLs open in a new tab.

## Requirements

Local projects need:

- Python 3.10 or newer
- Neovim with [VimTeX](https://github.com/lervag/vimtex)
- `synctex`
- a graphical web browser
- a LaTeX build toolchain

SSH projects need `ssh` and a browser locally. The remote host needs Python 3.10 or newer, Neovim with the user's normal configuration, `synctex`, and the LaTeX build toolchain. Neovim may be installed through an interactive shell setup; Paperhere discovers its absolute path before launching the non-interactive session.

## Architecture

```text
local project                         SSH project

Neovim ──HTTP──┐                     remote Neovim ──HTTP──┐
               │                                           │
        Paperhere agent                              remote agent
        ├─ builds / watches PDF                      ├─ SyncTeX
        ├─ runs SyncTeX                              └─ serves PDF/events
        └─ serves PDF/events                               │
               │                                    SSH local-forward
               └────────── browser viewer ─────────────────┘
```

The agent listens only on loopback and data endpoints require a random per-session token. On SSH launches, the current package is content-addressed and cached under `~/.cache/paperhere/bundles/` on the remote host. Runtime sockets live in a private temporary directory and the launcher removes the session processes when Neovim exits.

## Neovim plugin distribution

The plugin source lives in [`paperhere/nvim`](paperhere/nvim). The launcher always uses this nested runtime directly. A GitHub Action publishes that directory alone to the generated `nvim` branch, allowing lazy.nvim users to install only the plugin:

```lua
{
  "jongukc/paperhere",
  branch = "nvim",
  name = "paperhere.nvim",
  lazy = false,
  dependencies = { "lervag/vimtex" },
}
```

The `nvim` branch is generated and force-updated; do not edit it directly. See the [plugin README](paperhere/nvim/README.md) for local development configuration.

## Development

Run the unit suite and syntax checks:

```bash
python -m unittest discover -v
node --check paperhere/static/app.js
```

PDF.js is vendored under `paperhere/static/vendor`; its Apache 2.0 license is included alongside the distribution.
