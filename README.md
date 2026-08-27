# Canvas ICS → Taskwarrior + Obsidian Sync

> Single-file Python automation that turns a Canvas LMS calendar feed (`.ics`) into [Taskwarrior](https://taskwarrior.org/) tasks and [Obsidian](https://obsidian.md/) notes — daily or on-demand. No Canvas API token required.

**Author:** `gamemon` (GitHub) — vault maintained as `escproxy` · **Source repo:** `ObsidianVault` vault (`~/ObsidianVault`) · **Standalone tool:** this repo (`canvas_sync.py`)

---

## What it does

1. **Fetches** `https://uiowa.instructure.com/feeds/calendars/user_*.ics` (Canvas “Calendar Feed”, user-specific URL from Canvas → Calendar → Calendar Feed)
2. **Parses** RFC 5545 `.ics` — correctly unfolds folded lines (`\n `) and unescapes `\n \, \; \\`
3. **Maps** course codes in `SUMMARY` to Taskwarrior projects:
   - `CS:3620` → `os`
   - `MATH:3800` → `num-methods`
   - `PHYS:1512` → `phys`
   - `SJUS:1001` → `sjus` (else `canvas`)
4. **Deduplicates** — checks `task status:pending export` annotations for `canvas:<UID>` + fuzzy match on `due+project` (e.g., existing `NM HW01 due` ↔ `Homework #1 [MATH:3800]`), merges instead of duplicating
5. **Syncs to Taskwarrior** — `task add project:<proj> due:<YYYY-MM-DD> +canvas +assignment <title>` + annotations for `UID`, `URL`, description; tracks seen UIDs in `~/.task/canvas_sync_state.json`
6. **Generates Obsidian** — `!Schedule-Tasks/Canvas_Assignments.md` with frontmatter, sortable table, detail sections, and Dataview block (`WHERE contains(tags,"canvas")`)
7. **Automates** — `systemd` timer daily at `07:00` + 5 min after boot (`~/.config/systemd/user/canvas-sync.*`)

All logic is in **one file**: [`canvas_sync.py`](canvas_sync.py) (~400 LOC, stdlib only). `systemd/` timer/service + `install.sh` are included in this repo.

---

## Repo layout (standalone)

```
canvas-ics-sync/                  # ← Gamemon/canvas-ics-sync (this repo)
  canvas_sync.py                  # single-file logic (also copied to ObsidianVault/scripts/)
  README.md
  systemd/
    canvas-sync.service           # systemd unit (ExecStart → %h/canvas-ics-sync/canvas_sync.py)
    canvas-sync.timer             # OnCalendar=07:00, OnBootSec=5min, Persistent=true
  install.sh                      # copies units to ~/.config/systemd/user + enables timer
```

## Vault integration (ObsidianVault)

```
ObsidianVault/
  scripts/canvas_sync.py          # ← same file (canonical vault copy)
  canvas-ics-sync.sh              # thin Bash wrapper (curl + python)
  !Schedule-Tasks/
    Fall_2026_Schedule.md         # links to [[Canvas_Assignments]]
    Canvas_Assignments.md         # auto-generated (do not edit)
    timetree.ics                  # TimeTree export (separate)
```

`Fall_2026_Schedule.md` documents the workflow and tag scheme (`+canvas` all synced, `+assignment` vs `+calendar`).

---

## Requirements

- Python 3.10+ (stdlib only: `urllib`, `re`, `json`, `subprocess`, `pathlib`)
- `task` 3.x + `taskwarrior-tui` (optional, reads same `~/.task`)
- `curl` (optional, fetch fallback) and `systemd --user` for timer

No `icalendar`, no Canvas token, no cookies.

---

## Quick start

```bash
# 1. Clone standalone tool (or copy from vault)
git clone https://github.com/Gamemon/canvas-ics-sync.git
cd canvas-ics-sync

# 2. Preview (no writes)
python3 canvas_sync.py --dry-run --no-fetch   # uses /tmp/canvas_feed.ics
# or fresh fetch preview
python3 canvas_sync.py --dry-run

# 3. Real sync
python3 canvas_sync.py
# checks:  task +canvas list
# obsidian: cat ~/ObsidianVault/!Schedule-Tasks/Canvas_Assignments.md

# 4. Or via wrapper inside vault
~/ObsidianVault/canvas-ics-sync.sh --dry-run
~/ObsidianVault/canvas-ics-sync.sh
```

**Config:** edit `FEED_URL` at top of `canvas_sync.py` or set `CANVAS_FEED_URL` env for wrapper. `VAULT_ROOT` auto-detects `~/ObsidianVault`, else current dir.

---

## Daily automation (systemd timer included)

Units are versioned in [`systemd/`](systemd/):

- `systemd/canvas-sync.service` — `ExecStart=/usr/bin/python3 %h/canvas-ics-sync/canvas_sync.py` (vault variant commented)
- `systemd/canvas-sync.timer` — `OnCalendar=07:00`, `OnBootSec=5min`, `Persistent=true`, `RandomizedDelaySec=10min`

```bash
# Install from this repo (copies to ~/.config/systemd/user + enables)
./install.sh
# or manually:
mkdir -p ~/.config/systemd/user
cp systemd/canvas-sync.service systemd/canvas-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now canvas-sync.timer

# Verify
systemctl --user list-timers | grep canvas
systemctl --user status canvas-sync.service
cat ~/canvas-ics-sync/.canvas_sync.log   # standalone log
# vault log (if using vault copy):
# cat ~/ObsidianVault/.canvas_sync.log
journalctl --user -u canvas-sync.service -n 20
```

To change schedule: `systemctl --user edit canvas-sync.timer`. To use vault path, edit `ExecStart` in `~/.config/systemd/user/canvas-sync.service` to `.../ObsidianVault/scripts/canvas_sync.py`.

---

## Taskwarrior / TUI

```bash
task +canvas list           # all synced (5 pending as of 2026-08-27)
task +assignment list       # assignments only
task project:os list
task 12 annotate "note"     # add to merged task
task 46 done                # complete

# TUI (same data)
taskwarrior-tui
# inside TUI: / +canvas to filter, q to quit
```

Fuzzy merge example: existing `SJUS Learning Goals Short Paper due (2026-08-31, sjus)` + ICS `Learning Goals Short Paper (50 points) [SJUS:1001]` → annotated with `canvas:event-assignment-2845414` + `+canvas`, no duplicate.

---

## Obsidian

Open `!Schedule-Tasks/Canvas_Assignments.md`:

```markdown
| Due | Assignment | Project | Link |
|-----|------------|---------|------|
| 2026-08-28 | Homework 0 [CS:3620:0001 Fall26] | `os` | [ICON](https://...) |

```dataview
TABLE due as "Due", project as "Project", description as "Task"
FROM ""
WHERE contains(tags, "canvas")
SORT due ASC
```
```

Recreated on every sync; add personal notes in Taskwarrior or separate Obsidian pages and link via `[[Canvas_Assignments#Homework 0]]`.

---

## Troubleshooting

- **Feed 404/empty**: Canvas feed URL rotated — re-copy from Canvas → Calendar → Calendar Feed, update `FEED_URL`
- **Duplicates**: ensure `task status:pending export` shows annotations `canvas:<UID>`; check `~/.task/canvas_sync_state.json` and `task +canvas list`
- **Missing Obsidian note**: `VAULT_ROOT` mis-detected — set explicitly in script or run from vault dir
- **Timer not firing**: `systemctl --user daemon-reload && systemctl --user enable canvas-sync.timer && systemctl --user start canvas-sync.timer`

---

## Design notes

- Single-file, stdlib-only for portability (no `pip install` on lab machines)
- `unfold_ics` handles Canvas `DESCRIPTION:https://...␣  ` line folds; regex `BEGIN:VEVENT(.*?)END:VEVENT` after unfolding is robust
- `due_taskwarrior` is always `YYYY-MM-DD` (all-day `DTSTART;VALUE=DATE:20260828`); timed events like `zoom-testing` displayed as `2027-01-01 04:00 UTC` but tasked for that date
- State file prevents re-adding after `task delete` (pending-only dedup); completed tasks keep UID check

---

## License

MIT — do what you want, keep the `AUTHOR` line. PRs for more `PROJECT_MAP` entries welcome.
