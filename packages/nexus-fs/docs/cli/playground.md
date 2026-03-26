# nexus-fs playground

An interactive TUI file browser for exploring mounted backends in the
terminal.

## Install

The playground requires the `tui` extra:

```bash
pip install nexus-fs[tui]
```

This installs [Textual](https://textual.textualize.io/).

## Usage

```bash
# Mount specific backends
nexus-fs playground local://./data s3://my-bucket

# Auto-discover mounts from state directory
nexus-fs playground
```

When no URIs are provided, playground reads from the nexus-fs state
directory (`$TMPDIR/nexus-fs/mounts.json` by default, or
`$NEXUS_FS_STATE_DIR/mounts.json` if set) to find previously mounted
backends.

## Layout

The playground uses a two-panel layout:

```
┌─── Mount List ──────┬─── File Browser ────────────────────┐
│                      │                                     │
│ ▸ /local/data/       │  Name          Size    Modified     │
│   /s3/my-bucket/     │  ─────────────────────────────────  │
│                      │  📁 subdir/                         │
│                      │  📄 README.md   1.2 KB  2 hours ago │
│                      │  📄 data.csv    4.5 MB  yesterday   │
│                      │                                     │
├──────────────────────┴─────────────────────────────────────┤
│ Status: /local/data/ • 3 items • Press ? for help          │
└────────────────────────────────────────────────────────────┘
```

- **Left panel**: List of mounted backends. Select a mount to browse it.
- **Right panel**: File browser for the selected mount.
- **Status bar**: Current path, item count, and help hint.

The mount panel collapses automatically when the terminal is narrower
than 100 columns.

## Keyboard shortcuts

### Navigation

| Key | Action |
|-----|--------|
| ++up++ / ++down++ | Move selection |
| ++enter++ | Open directory / preview file |
| ++backspace++ | Go to parent directory |
| ++tab++ | Switch between mount list and file browser |

### Actions

| Key | Action |
|-----|--------|
| ++slash++ | Search files in current directory |
| ++escape++ | Clear search / close preview |
| `c` | Copy file path to clipboard |
| `r` | Rename selected file or directory |
| `d` | Delete selected file or directory |
| `n` | Create new file |
| ++shift+n++ | Create new directory |
| `q` | Quit |
| `?` | Show help |

### File preview

When you press ++enter++ on a file, playground shows a preview pane
with the file contents. Text files are displayed with syntax
highlighting. Binary files show a hex dump summary.

## Requirements

- Terminal width: minimum 80 columns
- Terminal height: minimum 24 rows
- Color: 256-color or truecolor terminal recommended
