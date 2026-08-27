#!/usr/bin/env python3
"""
Canvas ICS → Taskwarrior + Obsidian sync
- Fetches https://uiowa.instructure.com/feeds/calendars/user_*.ics
- Parses VEVENTs (handles RFC5545 line folding and \\n escapes)
- Maps course codes → taskwarrior projects
- Deduplicates via annotation containing Canvas UID
- Generates Obsidian note: !Schedule-Tasks/Canvas_Assignments.md
Usage:
  python3 scripts/canvas_sync.py [--dry-run] [--no-fetch] [--ics /tmp/canvas_feed.ics]
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

# --- Config ---
FEED_URL = "https://uiowa.instructure.com/feeds/calendars/user_*.ics"
VAULT_ROOT = Path(__file__).resolve().parent
# Detect vault location: if repo is standalone, try ~/ObsidianVault first, else current dir
if (Path.home() / "ObsidianVault" / "!Schedule-Tasks").exists():
    VAULT_ROOT = Path.home() / "ObsidianVault"
elif (Path(__file__).resolve().parent / "!Schedule-Tasks").exists():
    VAULT_ROOT = Path(__file__).resolve().parent
OBSIDIAN_OUT = VAULT_ROOT / "!Schedule-Tasks" / "Canvas_Assignments.md"
CACHE_ICS = Path("/tmp/canvas_feed.ics")
STATE_FILE = Path.home() / ".task" / "canvas_sync_state.json"

PROJECT_MAP = {
    "CS:3620": "os",
    "MATH:3800": "num-methods",
    "PHYS:1512": "phys",
    "SJUS:1001": "sjus",
    "SJUS": "sjus",
}

# Fallback project if no match
DEFAULT_PROJECT = "canvas"

def fetch_ics(url: str, dest: Path):
    print(f"[*] Fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = r.read()
            dest.write_bytes(data)
            print(f"[+] Saved {len(data)} bytes → {dest} (HTTP {r.status})")
            return True
    except Exception as e:
        print(f"[!] Fetch failed: {e}", file=sys.stderr)
        return False

def unfold_ics(raw: str) -> str:
    # RFC5545: lines starting with space/tab are continuations of previous line
    # First, normalize line endings
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Unfold: newline + space/tab → empty (continuation)
    unfolded = re.sub(r"\n[ \t]", "", raw)
    return unfolded

def unescape_ics_text(s: str) -> str:
    # ICS escapes: \n, \,, \;, \\, \N
    return s.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

def parse_events(ics_text: str):
    unfolded = unfold_ics(ics_text)
    # Split into VEVENT blocks
    blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, flags=re.DOTALL)
    events = []
    for block in blocks:
        def get_field(name):
            # Match FIELD or FIELD;PARAMS: value
            m = re.search(rf"^{name}(?:;[^:]*)?:(.*)$", block, flags=re.MULTILINE)
            return unescape_ics_text(m.group(1).strip()) if m else None

        uid = get_field("UID")
        summary = get_field("SUMMARY")
        dtstart = get_field("DTSTART")
        dtend = get_field("DTEND")
        description = get_field("DESCRIPTION")
        url = get_field("URL")
        location = get_field("LOCATION")
        dtstamp = get_field("DTSTAMP")

        # Raw DTSTART line for date parsing (need params)
        dtstart_raw = re.search(r"^DTSTART(?:;[^:]*)?:(.*)$", block, flags=re.MULTILINE)
        dtstart_raw = dtstart_raw.group(0) if dtstart_raw else ""

        if not summary or not uid:
            continue

        # Parse due date
        due_fmt = None
        due_taskwarrior = None  # YYYY-MM-DD for taskwarrior
        # Try YYYYMMDD (all-day)
        m_date = re.search(r"(\d{8})(?:T(\d{6})Z?)?", dtstart or "")
        if m_date:
            ymd = m_date.group(1)
            t = m_date.group(2)
            due_fmt = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
            due_taskwarrior = due_fmt
            # If has time, keep as date still (task due = that day)
            # For event with time like zoom-testing, preserve time for obsidian but task due is still date
            if t and "T" in dtstart_raw:
                # e.g., 20270101T040000Z → display as 2027-01-01 04:00 UTC
                try:
                    dt = datetime.strptime(ymd + t, "%Y%m%d%H%M%S")
                    # Assume Z = UTC
                    due_fmt = dt.strftime("%Y-%m-%d %H:%M UTC")
                except:
                    pass

        # Determine project
        proj = DEFAULT_PROJECT
        for code, p in PROJECT_MAP.items():
            if code in summary:
                proj = p
                break

        # Clean description for task annotation (first line, truncate)
        desc_short = ""
        if description:
            desc_short = description.split("\n")[0].strip()[:200]

        events.append({
            "uid": uid,
            "summary": summary.strip(),
            "dtstart_raw": dtstart or "",
            "dtstart_line": dtstart_raw,
            "due": due_fmt or "TBA",
            "due_taskwarrior": due_taskwarrior,
            "description": description or "",
            "url": url or "",
            "location": location or "",
            "project": proj,
            "is_assignment": "assignment" in uid or "Homework" in summary or "HW" in summary or "Paper" in summary or "Quiz" in summary or "Lab" in summary,
        })
    return events

def load_existing_tasks():
    try:
        # Export all non-deleted tasks — pending+completed should block re-add, deleted should not
        out = subprocess.check_output(["task", "export"], text=True, timeout=10)
        tasks = json.loads(out)
        # Filter out deleted explicitly (task export includes deleted with status=deleted)
        tasks = [t for t in tasks if t.get("status") != "deleted"]
        return tasks
    except Exception as e:
        print(f"[!] task export failed: {e}", file=sys.stderr)
        return []

def task_has_uid(tasks, uid: str) -> bool:
    # Check annotations for UID — pending/completed/waiting block re-add, deleted already filtered
    for t in tasks:
        if t.get("status") == "deleted":
            continue
        for ann in t.get("annotations", []):
            if uid in ann.get("description", ""):
                return True
        if uid in t.get("description", ""):
            return True
        if t.get("canvas_uid") == uid:
            return True
    return False

def find_fuzzy_match(tasks, ev):
    """Find existing pending task that likely represents same assignment (same due + project)."""
    due = ev["due_taskwarrior"]
    proj = ev["project"]
    # Normalize summary for keyword matching
    summary_low = ev["summary"].lower()
    # Extract number from summary e.g. Homework 0 / Homework #1 / HW01
    m_num = re.search(r"homework\s*#?\s*0*(\d+)", summary_low)
    hw_num = m_num.group(1) if m_num else None

    for t in tasks:
        if t.get("status") != "pending":
            continue
        if t.get("project") != proj:
            continue
        t_due = t.get("due", "")[:10]  # task due is ISO like 20260904T...
        # Normalize due: task export due is like 20260904T000000Z or 2026-09-04T...
        # We'll compare YYYY-MM-DD
        try:
            # task due may be "20260831T000000Z" or with T
            if "T" in t_due:
                t_due_day = t_due.split("T")[0]
            else:
                t_due_day = t_due
            # Convert YYYYMMDD to YYYY-MM-DD if needed
            if len(t_due_day) == 8 and "-" not in t_due_day:
                t_due_day = f"{t_due_day[:4]}-{t_due_day[4:6]}-{t_due_day[6:]}"
            elif len(t_due_day) == 10 and t_due_day[4] == "-":
                pass
            else:
                # fallback: try parse via datetime
                t_due_day = t_due_day[:10]
        except:
            continue
        # Compare due day
        if t_due_day != due:
            continue
        # Due + project matches — now check description similarity
        desc_low = t.get("description", "").lower()
        # Strong signals: same hw number, or shared keywords
        if hw_num and hw_num in desc_low and ("hw" in desc_low or "homework" in desc_low):
            return t
        # Learning Goals Short Paper fuzzy
        if "learning goals" in summary_low and "learning goals" in desc_low:
            return t
        # Generic: if 2+ words from summary appear in task description
        # Extract words from summary without bracket
        summary_core = re.sub(r"\[.*?\]", "", summary_low)
        words = [w for w in re.findall(r"[a-z]{3,}", summary_core) if w not in ("fall26", "fall")]
        overlap = sum(1 for w in words if w in desc_low)
        if overlap >= 2:
            return t
        # Fallback: if only due+project matches and project is not generic, consider it duplicate
        # For phys/sjus with single assignment per due date, this is safe.
        # But to be safe, only auto-merge if task doesn't already have canvas tag (manual task)
        if "canvas" not in t.get("tags", []):
            # Treat as potential duplicate — annotate but warn
            return t
    return None

def sync_to_taskwarrior(events, dry_run=False):
    tasks = load_existing_tasks()
    pending_uids = {a["description"] for t in tasks for a in t.get("annotations", [])}

    added = 0
    skipped = 0
    merged = 0
    for ev in events:
        uid = ev["uid"]
        # Skip non-assignment calendar events? Keep all but tag appropriately
        # For zoom-testing etc, still add but with lower priority? Filter to keep useful
        # We currently sync all events that have a due date
        if not ev["due_taskwarrior"]:
            skipped += 1
            continue

        if task_has_uid(tasks, uid):
            # Already synced
            skipped += 1
            continue

        # Also check state file
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                if uid in state.get("synced_uids", []):
                    # double-check task still exists; if not, re-add
                    if any(uid in ann.get("description", "") for t in tasks for ann in t.get("annotations", [])):
                        skipped += 1
                        continue
            except:
                pass

        # Fuzzy dedup: check if manual syllabus task already exists for same due+project
        fuzzy = find_fuzzy_match(tasks, ev)
        if fuzzy:
            tid = fuzzy.get("id")
            tdesc = fuzzy.get("description")
            print(f"  ~ Merging {uid} into existing task {tid}: \"{tdesc[:50]}\" (same due {ev['due_taskwarrior']} project {ev['project']})")
            if not dry_run and tid:
                # Annotate existing task with Canvas metadata if not already annotated
                has_uid = any(uid in a.get("description","") for a in fuzzy.get("annotations",[]))
                if not has_uid:
                    subprocess.run(["task", str(tid), "annotate", f"canvas:{uid}"], capture_output=True, timeout=10)
                    if ev["url"]:
                        subprocess.run(["task", str(tid), "annotate", ev["url"]], capture_output=True, timeout=10)
                    # Add canvas tag if missing
                    if "canvas" not in fuzzy.get("tags", []):
                        subprocess.run(["task", str(tid), "modify", "+canvas"], capture_output=True, timeout=10)
                # Update state
                state = {}
                if STATE_FILE.exists():
                    try:
                        state = json.loads(STATE_FILE.read_text())
                    except:
                        state = {}
                state.setdefault("synced_uids", [])
                if uid not in state["synced_uids"]:
                    state["synced_uids"].append(uid)
                state["last_sync"] = datetime.now(timezone.utc).isoformat()
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATE_FILE.write_text(json.dumps(state, indent=2))
            merged += 1
            skipped += 1
            continue

        proj = ev["project"]
        due = ev["due_taskwarrior"]
        title = ev["summary"].replace('"', "'")  # avoid shell quoting issues
        # Build task add command — tags must be separate args
        tags = ["+canvas"]
        if "assignment" in uid:
            tags.append("+assignment")
        else:
            tags.append("+calendar")

        # Use taskwarrior python invocation via subprocess with JSON import style
        # Safer to use `task add` with proper escaping via list args (no shell)
        cmd = ["task", "add", f"project:{proj}", f"due:{due}"] + tags + [title]
        print(f"  -> {' '.join(cmd)}")
        print(f"     UID:{uid} URL:{ev['url'][:60]}")

        if not dry_run:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode != 0:
                    print(f"[!] task add failed: {result.stderr}", file=sys.stderr)
                    continue
                # Extract created task UUID? task add prints "Created task X."
                # Annotate with UID and URL for dedup
                # Find newest task with this description
                # Simpler: use `task +canvas` and annotate last added
                # Get ID of last added task: parse result
                m = re.search(r"Created task (\d+)", result.stdout)
                if m:
                    task_id = m.group(1)
                    # Annotate with UID
                    subprocess.run(["task", task_id, "annotate", f"canvas:{uid}"], capture_output=True, timeout=10)
                    if ev["url"]:
                        subprocess.run(["task", task_id, "annotate", ev["url"]], capture_output=True, timeout=10)
                    if ev["description"]:
                        # Add first line of description as annotation (truncated)
                        short = ev["description"].split("\n")[0][:200]
                        if short and short not in title:
                            subprocess.run(["task", task_id, "annotate", short], capture_output=True, timeout=10)
                # Update state file
                state = {}
                if STATE_FILE.exists():
                    try:
                        state = json.loads(STATE_FILE.read_text())
                    except:
                        state = {}
                state.setdefault("synced_uids", [])
                if uid not in state["synced_uids"]:
                    state["synced_uids"].append(uid)
                state["last_sync"] = datetime.now(timezone.utc).isoformat()
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATE_FILE.write_text(json.dumps(state, indent=2))
                added += 1
                # Refresh tasks list for next dedup check
                tasks = load_existing_tasks()
            except Exception as e:
                print(f"[!] Error adding task: {e}", file=sys.stderr)
        else:
            added += 1

    print(f"[*] Taskwarrior sync: {added} added, {merged} merged, {skipped} skipped")
    return added, skipped

def generate_obsidian(events, dry_run=False):
    # Sort by due date
    def sort_key(e):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", e["due"] or "")
        return m.group(1) if m else "9999-99-99"
    events_sorted = sorted(events, key=sort_key)

    # Build markdown
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append("---")
    lines.append("tags:")
    lines.append("  - canvas")
    lines.append("  - assignments")
    lines.append("  - auto-generated")
    lines.append(f"updated: {now}")
    lines.append(f"source: {FEED_URL}")
    lines.append("---")
    lines.append("")
    lines.append("# Canvas Assignments — Auto-synced")
    lines.append("")
    lines.append(f"> [!info] Last synced: {now} | Source: [Canvas ICS]({FEED_URL}) | Events: {len(events)}")
    lines.append(f"> Auto-generated by `scripts/canvas_sync.py`. Do not edit manually — your edits will be overwritten. Add notes in taskwarrior or link via `[[...]]`.")
    lines.append("")
    lines.append("## Taskwarrior Quick Links")
    lines.append("")
    lines.append("```bash")
    lines.append("# View synced tasks")
    lines.append("task +canvas list")
    lines.append("task +assignment list")
    lines.append("# Re-sync manually")
    lines.append("./canvas-ics-sync.sh")
    lines.append("python3 scripts/canvas_sync.py")
    lines.append("```")
    lines.append("")
    lines.append("## Upcoming Assignments (from Canvas ICS)")
    lines.append("")
    lines.append("| Due | Assignment | Project | Link |")
    lines.append("|-----|------------|---------|------|")
    for ev in events_sorted:
        due = ev["due"]
        title = ev["summary"].replace("|", "\\|")
        proj = ev["project"]
        link = ev["url"]
        # Make obsidian link if url present
        if link:
            link_md = f"[ICON]({link})"
        else:
            link_md = ""
        # Shorten title for display
        lines.append(f"| {due} | {title} | `{proj}` | {link_md} |")

    lines.append("")
    lines.append("## Details")
    lines.append("")
    for ev in events_sorted:
        lines.append(f"### {ev['summary']}")
        lines.append(f"- **UID:** `{ev['uid']}`")
        lines.append(f"- **Due:** {ev['due']}")
        lines.append(f"- **Project:** `{ev['project']}`")
        if ev["url"]:
            lines.append(f"- **URL:** {ev['url']}")
        if ev["location"]:
            lines.append(f"- **Location:** {ev['location']}")
        if ev["description"]:
            # First 500 chars, escape
            desc = ev["description"][:500].replace("\n", " ").strip()
            if desc:
                lines.append(f"- **Description:** {desc}")
        lines.append("")

    lines.append("---")
    lines.append("## Dataview (tasks with +canvas)")
    lines.append("")
    lines.append("```dataview")
    lines.append('TABLE due as "Due", project as "Project", description as "Task"')
    lines.append('FROM ""')
    lines.append('WHERE contains(tags, "canvas")')
    lines.append("SORT due ASC")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append(f"*Generated: {now} — run `python3 scripts/canvas_sync.py` to refresh*")
    content = "\n".join(lines)

    if dry_run:
        print("\n=== Obsidian preview (first 40 lines) ===")
        print("\n".join(lines[:40]))
        return content

    OBSIDIAN_OUT.parent.mkdir(parents=True, exist_ok=True)
    OBSIDIAN_OUT.write_text(content, encoding="utf-8")
    print(f"[+] Wrote Obsidian note → {OBSIDIAN_OUT} ({len(content)} bytes)")
    return content

def main():
    ap = argparse.ArgumentParser(description="Canvas ICS → Taskwarrior + Obsidian")
    ap.add_argument("--dry-run", action="store_true", help="Don't modify taskwarrior or obsidian, just print")
    ap.add_argument("--no-fetch", action="store_true", help="Skip fetching, use cached /tmp/canvas_feed.ics")
    ap.add_argument("--ics", type=str, default=str(CACHE_ICS), help="Path to ICS file")
    ap.add_argument("--no-taskwarrior", action="store_true", help="Skip taskwarrior sync")
    ap.add_argument("--no-obsidian", action="store_true", help="Skip obsidian generation")
    args = ap.parse_args()

    ics_path = Path(args.ics)

    if not args.no_fetch:
        ok = fetch_ics(FEED_URL, ics_path)
        if not ok and not ics_path.exists():
            print("[!] No cached ICS and fetch failed — abort", file=sys.stderr)
            sys.exit(1)
        if not ok:
            print("[*] Using cached ICS due to fetch failure")

    if not ics_path.exists():
        print(f"[!] ICS file not found: {ics_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Parsing {ics_path} ({ics_path.stat().st_size} bytes)")
    text = ics_path.read_text(encoding="utf-8", errors="ignore")
    events = parse_events(text)
    print(f"[+] Parsed {len(events)} events:")
    for ev in events:
        print(f"  - {ev['due']:16} | {ev['project']:12} | {ev['summary'][:60]} | {ev['uid']}")

    if not args.no_taskwarrior:
        sync_to_taskwarrior(events, dry_run=args.dry_run)
    else:
        print("[*] Skipped Taskwarrior sync")

    if not args.no_obsidian:
        generate_obsidian(events, dry_run=args.dry_run)
    else:
        print("[*] Skipped Obsidian generation")

if __name__ == "__main__":
    main()
