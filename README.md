# ccwidget - The Claude Code Monitoring Widget

An animated terminal dashboard of every Claude Code session running on this machine: what each one is doing, which ones are waiting on you, their sub-agents, your plan usage, and the projects you've worked on today. It also comes as **ccwidget**, a macOS menu bar app and floating widget.

Source: [github.com/miong13/ccwidget](https://github.com/miong13/ccwidget). The ready-built app (`.dmg` / `.zip`) is on the [Releases](https://github.com/miong13/ccwidget/releases) page. It isn't notarized, so read `Open Me First.txt` inside the download before opening it (see [Packaging](#packaging)).

```
  CLAUDE COMMAND CENTER   3 sessions · 1 working · 2 sub-agents   ▲ 1 needs you          03:37:36

╭─ USAGE ──────────────────────────────────────────────────────────────────────────────────────╮
│ 5-hour  ▎░░░░┃░░░░░░░░░░░░░░░░░░░░░░   1%   resets in 4h17m   on pace for ~7% by reset      │
│ weekly  ██░░░░░░░░░░░░░┃░░░░░░░░░░░░   6%   resets Tue 04:00   on pace for ~11% by reset    │
│ today   │▁▁▁█····················│   120.7k out   489.6k processed   +8.9M cache reads (96%) │
│ models  opus 5.5 ███████▋ 98%   haiku 4.5 ▏······· 2%                                         │
│ cost    ≈$7.03 today (2 sessions)   ≈$7.03 in open sessions                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ PROJECTS TODAY ──────────────────────────────────────────────── 3h01m active · since 08:12 ─╮
│   project          time  0     6     12    18      prompts      lines  files  commits        │
│ ⠇ api-server      2h05m  ▁▁▁▁▁▁▁▁▁▃█▆▂▅···········       23  +1.2k -96     14        3       │
│ ▲ infra             48m  ▁▁▁▁▁▁▁▁▁▁▂▅▁▃···········        9    +86 -40      3        1       │
│ · notes             12m  ▁▁▁▁▁▁▁▁▂▁▁▁▁▁···········        2          ·      ·        ·       │
╰──────────────────────── 3 projects · 4 sessions · 34 prompts · +1.3k -136 lines · 4 commits ─╯
╭─ deploy-bot ───────────────── △ NEEDS YOU ─╮ ╭─ api-refactor ──────━──────────── ⠇ WORKING ─╮
│     !      Deploy staging                  │ │     ●      Refactor auth middleware          │
│  ╭─────╮ o ▸ infra                         │ │  ╭─────╮   ▸ api-server                      │
│  │ ◉ ◉ │╱  approve ⚒ Bash                  │ │  │ ◉ ◉ │   now  ⚒ Edit                       │
│  ╰──┬──╯        Apply production manifest  │ │  ╰──┬──╯        middleware.ts                │
│  ╱ ███     waiting 4s · up 10m04s          │ │  ─ ███ ╲   working 1m37s · up 1h00m          │
│  ▔▔▔▔▔▔▔   io ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁         │ │ ▀▄▀▄▀▄▀▄▀  io ▁▁▂▅▇▃▁▁▁▂▁ ◆ 2/2 agents      │
│            ctx ▌░░░░░░░░░   7% · $1.10     │ │            ctx ▊░░░░░░░░░   9% · $1.60       │
╰────────────────────────────────────────────╯ │  └ ⠇ Run test suite                          │
                                               │  └ ⠙ Review token refresh logic              │
                                               ╰──────────────────────────────────────────────╯
 q quit · i hide idle · u compact usage
```

## Quick start

```sh
python3 ccwatch.py          # live dashboard
python3 ccwatch.py --once   # one-shot plain-text snapshot, projects included (good for scripts / ssh)
python3 ccwatch.py --json   # one JSON snapshot per second (feeds the desktop widget); add --once for just one
python3 ccwatch.py --focus <pid>   # bring the terminal/editor window running that claude process to the front
python3 ccwatch.py --install       # add the hook and status line capture to ~/.claude (--dry-run to preview)
python3 ccwatch.py --doctor        # check every data source and say what's missing
./build-widget.sh && open ccwidget.app   # floating desktop widget (see below)
./package.sh                             # self-contained ccwidget.app + .zip + .dmg in dist/ (see Packaging)
```

On startup it shows "scanning today's Claude transcripts…" for a second or two while it reads today's history; after that it only reads what's new.

Requirements: macOS (tested; Linux should work but hasn't been tried), Python 3.8+ (standard library only, no `pip install`), and `jq` for the hook and status line. Run it in its own terminal tab or split pane; it's read-only and safe to leave open.

**First time on a Mac:** run `python3 ccwatch.py --install` once. It wires up the two things ccwatch can't read on its own: the hook that says when a session needs you, and the status line capture that carries plan limits, context and cost. See [Setup](#setup).

| Key | Action |
|---|---|
| `q` / `Esc` | Quit |
| `i` | Hide / show idle sessions |
| `u` | Switch the usage and projects panels between full and compact |

## Files

| File | Role |
|---|---|
| `ccwatch.py` | The dashboard. Reads local files only; the only things it deletes are stale files in its own `~/.cache/ccwatch`. `--install` / `--uninstall` are the only modes that edit anything else. |
| `ccwatch-hook.sh` | Hook script that records when a session is blocked waiting on you. |
| `ccwidget.swift` | Source of the floating desktop widget. |
| `build-widget.sh` | Compiles `ccwidget.swift` into `ccwidget.app` in this folder (dev build; runs the `ccwatch.py` beside it). |
| `package.sh` | Builds a self-contained, universal `dist/ccwidget.app` with `ccwatch.py` inside, plus a `.zip` and a `.dmg`. |
| `dist/` | Output of `package.sh` (git-ignored, like `ccwidget.app`). |
| `VERSION` | The project version (`1.2.1`). Shown in *About ccwidget* and used by `package.sh` as the default version. |
| `avatar.png` | The author's picture for *About ccwidget*, copied into the app at build time. |
| `author.conf` | Author name and email for *About ccwidget*. |
| `LICENSE` | MIT license. |
| `tests/` | Unit tests, standard library only: `python3 -m unittest discover -s tests`. |
| `~/.claude/statusline-command.sh` | Your status line script, with a few lines added that save Claude Code's status data for the dashboard (or `~/.claude/ccwatch-statusline.sh` if you had none). |
| `~/.claude/settings.json` | Hook entries that run `ccwatch-hook.sh`, added alongside any hooks you already have. |

## Desktop widget

`ccwidget.app` shows the same live data in two places: a small panel that floats above your other windows (it stays visible on every Space and over full-screen apps and never takes keyboard focus, so it can sit in a corner while you work), and a menu bar icon that opens the same view in a drop-down popover. A third piece, the *hovering assistant*, appears only when a session needs you.

```sh
./build-widget.sh        # needs Xcode or the Command Line Tools (swiftc); takes about a minute
open ccwidget.app
```

- **Menu bar icon** is a small robot that shows your sessions and your plan limits at a glance:

  | Part | Shows |
  |---|---|
  | **Corner dot** | No dot: every session is idle. Green, with the number working beside the icon: at least one session is running. Blinking orange, with the number waiting in orange: a session needs you; this wins over green. |
  | **Robot colour** | Weekly limit used. White on a dark menu bar (black on a light one) below 50%, amber from 50%, red from 90%. |
  | **Bar under the robot** | 5-hour limit used, filling left to right, with the same colours (amber from 50%, red from 90%). Once it's at 100%, a red `↻ 1h20m` beside the icon counts down to the reset. |

  The robot colour and the bar appear once any session's status line has reported plan limits (see *How it works*). Until then, the robot is plain with no bar.

  Hover over it for a summary: sessions, plus each limit's percentage and time to reset. **Click** it to drop down the full view under the icon. Everything works there as in the floating panel: click a session to jump to it, use the chevron, hover over projects. Click the icon again, or anywhere else, to close it. **Right-click** the icon for the same options menu as the panel.

  **To move it**, for example next to the battery, hold ⌘ and drag it along the menu bar, then let go where you want it.
  - Apps can't choose their own position, so this has to be done by hand.
  - macOS remembers the spot across restarts, rebuilds and updates. It's stored in the widget's settings, so `defaults delete local.ccwatch.widget` resets it.
  - Whether it can sit right of the battery, among the system icons, depends on the macOS version; if it won't go there, put it just left of the battery.
  - On a crowded MacBook menu bar the icon can end up hidden behind the notch; ⌘-drag it further right.
- **Hovering assistant** is a small robot, in the spirit of Microsoft's Clippy, that hovers on the desktop holding up a card of the sessions waiting on you. These include a permission prompt, a question, a plan to approve, or an MCP input request.
  - **When it appears:** as soon as any session needs you. It fades in and hovers in place, bobbing gently on its thruster.
  - **When it leaves:** clicking a session doesn't close it. It stays until every waiting session has been answered, and each answered one drops off the card within a second or so. When the last one goes:
    1. The card turns green with *All caught up*, and the robot smiles.
    2. A moment later, its thruster fires and it flies up off the top of the screen.

    If another session needs you while it's flying off, it turns around and comes back down.
  - **What the card shows:** each waiting session's title, what it's asking for, how long it has waited and its project. With more than four waiting, it lists four and adds *+N more*.
  - **Click a session** on the card to jump to the window it runs in, just like a session tile (see *Click a session* below).
  - **×** sends it flying off straight away, until a *different* session needs you. The ones you dismissed don't bring it back.
  - **Where it appears:** always in the bottom-right corner of the display you're working on, clear of the Dock, with the card growing upward from there. That's the display holding the frontmost app's front window, or failing that the one under the pointer, then the main display. If you switch to an app on another display while it's showing, it moves to that display's corner. You can drag it out of the way for the moment, but it comes back to the corner next time. It floats above other windows on every Space and never takes keyboard focus.
  - **Show hovering assistant** (in either right-click menu, on by default) turns it off or on. The choice is remembered.
  - It works whether the floating panel is shown or hidden.
- **Show floating widget** (in either right-click menu) hides or shows the floating panel; the menu bar icon stays either way. The choice is remembered, so if you prefer the menu bar alone, the panel stays hidden on the next launch.
- **Move** it by dragging anywhere on it; the position is remembered. It grows and shrinks to fit, keeping its top edge in place.
- **Appearance** follows macOS Light/Dark mode: a solid near-white or near-black card, switching live when the system does.
- **Monitor icons** show each session's state: green with lines of script typing and scrolling while it works, flashing yellow with a `!` when it needs you, dark with a blinking prompt when idle. Sub-agents get small purple ones, and the header icon shows the overall state.
- **Session rows** show a small context-window gauge (green, yellow from 50%, red from 80%: time to `/compact`). In compact mode it only appears from 80%. Hover a row for its model, uptime, context and cost.
- **Right-click a session** (in the panel, the popover or the assistant's card) for *Jump to Window*, *Copy Resume Command* (`cd '<project>' && claude --resume <id>`, ready to paste), *Reveal Project in Finder* and *Reveal Transcript in Finder*, above the usual menu.
- **Notifications** (right-click menu → *Notifications*):
  - *When a Long Turn Finishes* (on): a session that worked for 2 minutes or more goes idle. Change the threshold with `defaults write local.ccwatch.widget notifyAfter -int <seconds>`.
  - *When a Session Needs You* (off, since the hovering assistant covers it).
  - *Plan Limit Warnings* (on): a limit window crosses 80%, 95% or 100%, once per window, even across restarts.

  macOS asks for permission with the first one. Clicking a session's notification jumps to its window.
- **Global shortcuts** (on by default; toggle with *Global Shortcuts* in the menu): **⌃⌥⌘J** jumps to the session that has waited longest, or opens the popover when none is waiting; **⌃⌥⌘W** shows or hides the floating panel. They need no Accessibility permission. If another app already uses one, that one just doesn't register.
- **Set-up prompt:** when the hook or the status line capture is missing (say, on a new Mac), a yellow row at the top says what's missing, with a *Set up…* button. It shows what will change in `~/.claude` and asks before doing it. The same is in the menu as *Set Up Hooks & Status Line…*, next to *Run Diagnostics…* (`ccwatch.py --doctor`).
- **Chevron** (top right) switches between full and compact. Compact shows only sessions that are working or need you, the two limit bars, and the top three projects.
- **Right-click** for *About ccwidget*, compact/expand, the floating panel's opacity, *Show floating widget*, *Show hovering assistant*, *Notifications*, *Global Shortcuts*, *Open full dashboard* (opens `ccwatch.py` in a new iTerm window, or Terminal if iTerm isn't installed; macOS asks once for permission), *Set Up Hooks & Status Line…*, *Run Diagnostics…* and *Quit ccwidget*.
- **About ccwidget** (top of either right-click menu) opens a small window with:
  - the author's avatar, name and email (click the email to write one);
  - the version, plus a *dev build* tag when the app runs the `ccwatch.py` beside it instead of a bundled copy.

  `build-widget.sh` fills these in when it builds the app:
  - **Name and email:** taken from the first of these that is set:
    1. the `AUTHOR_NAME` / `AUTHOR_EMAIL` environment variables;
    2. `author.conf` (currently Mark Ramos, miong13@gmail.com);
    3. `git config user.name` / `user.email`.

    They're stored in Info.plist as `CCAuthorName` / `CCAuthorEmail`. `author.conf` is there so the About window can show a personal email without changing the git identity used for commits.
  - **Avatar:** `avatar.png` from this folder, copied into the app. It's drawn in a circle on a white backing, so transparent pictures stay legible in Dark mode. Without it, the window shows the author's initials.
  - **Version:** from the `VERSION` file.

  To use a different picture, replace `avatar.png` (square, about 256 px) and rebuild. The current one is the Google account picture for miong13@gmail.com. It was downloaded at 512 px from the `lh3.googleusercontent.com` URL that Chrome saves in `~/Library/Application Support/Google/Chrome/<profile>/Preferences`, then scaled to 256 px:
  ```sh
  curl -sSfL -o /tmp/google.png "<picture URL without its =s… suffix>=s512-c" && sips -Z 256 /tmp/google.png --out avatar.png
  ```
  To use the macOS account picture instead:
  ```sh
  dscl . -read /Users/$USER JPEGPhoto | tail -n +2 | xxd -r -p > /tmp/me.jpg && sips -s format png -Z 256 /tmp/me.jpg --out avatar.png
  ```
- **Click a session** to jump to the window it runs in. The pointer turns into a hand over a session tile. ccwatch finds the app hosting the `claude` process from its parent processes, then:
  - **iTerm2 / Terminal:** the exact window and tab, matched by the session's tty.
  - **Cursor / VS Code / Windsurf / VSCodium:** the project window open on the session's folder, from the editor's saved window list. It can't pick the terminal panel inside that window.
  - **Android Studio and other JetBrains IDEs:** the project window, found by its title through System Events.
  - **Anything else:** the app comes forward.

  If the tab or window can't be found, it only activates the app; a click never opens a new window or project. macOS asks once for permission to control iTerm/Terminal or System Events. JetBrains windows also need ccwidget under *System Settings → Privacy & Security → Accessibility*; re-add it there after a rebuild, since the ad-hoc signature changes. The same jump is available from the shell as `ccwatch.py --focus <pid>`.
- **Hover** over a project for its branch, sessions, files, lines, commits and cost.
- **Start at login:** System Settings → General → Login Items → add `ccwidget.app`.

It has no Dock icon or app menu, only the menu bar icon. To stop it, choose *Quit ccwidget* from either right-click menu, or run `pkill -f ccwidget.app`.

How it works: the widget runs `ccwatch.py --json` as a child process and redraws from each snapshot, so it shows exactly what the terminal dashboard does and all scanning stays in one place. It uses the first `python3` it finds in `/opt/homebrew/bin`, `/usr/local/bin` or `/usr/bin` (apps opened from Finder don't get your shell's `PATH`), and runs the `ccwatch.py` bundled inside the app if there is one (a `package.sh` build), otherwise the one next to the app (a `build-widget.sh` build), so keep a dev build in this folder, or set `CCWATCH_PY`. If the feed stops, the widget shows the error and restarts it, after 3 seconds at first and backing off to a minute while it keeps failing. It animates at 10 fps only while a session is working or needs you, at 2 fps when all are idle, and not at all while the panel or popover is hidden. For a dev build, rebuild after changing `ccwidget.swift`; changes to `ccwatch.py` only need the widget restarted. A packaged app needs `./package.sh` again for either.

## Packaging

`./package.sh` turns the widget into a standalone Mac app you can keep in /Applications or give to someone else. Nothing needs to stay next to it.

```sh
./package.sh              # version from the VERSION file, e.g. 1.0.0
./package.sh 1.2.0        # or give one (bump VERSION too, so dev builds and About agree)
```

It writes to `dist/`:

| Output | What it is |
|---|---|
| `ccwidget.app` | Universal app (Apple Silicon and Intel), macOS 13 or later, with the robot app icon |
| `ccwidget-<version>.zip` | A `ccwidget-<version>` folder with the app and `Open Me First.txt`, for sharing as an attachment |
| `ccwidget-<version>.dmg` | Disk image with the app, an Applications shortcut and `Open Me First.txt`; open it and drag the app across |

What it does:

1. Checks that `ccwatch.py` parses.
2. Runs `build-widget.sh` with `APP=dist/ccwidget.app ARCHS="arm64 x86_64" VERSION=<version>`. This compiles both architectures and merges them with `lipo`.
3. Copies `ccwatch.py`, `ccwatch-hook.sh` and this README into `Contents/Resources/`. `build-widget.sh` has already added `avatar.png` and the author details for *About ccwidget*.
4. Draws the app icon (`ccwidget --iconset`, then `iconutil`).
5. Signs the app, verifies the signature, then builds the zip and the dmg.

The app looks for `ccwatch.py` in three places, in this order:
1. `$CCWATCH_PY`;
2. its own `Contents/Resources/`;
3. the folder next to the app.

So a packaged app always runs the copy it was built with. A dev build from `build-widget.sh` has no bundled copy and keeps using the one beside it. **Re-run `./package.sh` after changing `ccwatch.py` or `ccwidget.swift`** to pick up the change. The app runs Python with `-B`, so it never writes `__pycache__` into the bundle and its signature stays valid.

**Installing on a Mac:** open the dmg and drag `ccwidget.app` to Applications, then open it.

- **Python 3.8+:** the target Mac needs it at `/opt/homebrew/bin`, `/usr/local/bin` or `/usr/bin`. A fresh Mac's `/usr/bin/python3` asks to install the Command Line Tools the first time; accept, then reopen the app.
- **Hook and status line:** these are separate. Without them the widget still lists sessions and today's projects, but shows no "needs you" state and no plan-limit bars. Click *Set up…* in the widget (or *Set Up Hooks & Status Line…* in its menu). The app copies its `ccwatch-hook.sh` to `~/.claude/`, so later updates of the app don't break the path, then adds the hook entries and the status line capture (see [Setup](#setup)).

**Signing:** by default the app is signed ad-hoc. That is fine on the Mac that built it. A copy received any other way, such as WhatsApp, a browser download, email or AirDrop, is marked as quarantined. macOS then blocks its first launch with *"ccwidget" Not Opened — Apple could not verify "ccwidget" is free of malware…*. The app isn't damaged; macOS just can't check who made it. The recipient should click **Done**, not *Move to Trash*, and allow it once in one of two ways:
- *System Settings → Privacy & Security*, scroll to "ccwidget was blocked…", then **Open Anyway**, enter the password, and **Open Anyway** again (macOS 15 and later no longer offer right-click → Open for this);
- in Terminal, `xattr -dr com.apple.quarantine /Applications/ccwidget.app`, then open it normally.

These steps are also in `Open Me First.txt`, which `package.sh` puts next to the app in both the zip and the dmg. It's left out when the build is notarized, since a notarized app needs no workaround.

Only a **Developer ID Application** certificate plus notarization removes the warning. That needs the paid Apple Developer Program. The free *Apple Development* certificate that Xcode creates for a personal Apple ID doesn't help: Gatekeeper treats apps signed with it like ad-hoc ones.

Ad-hoc signatures also change on every build, so macOS asks again for the Automation and Accessibility permissions after an update. With an Apple Developer account you can avoid both:

```sh
SIGN_ID="Developer ID Application: Your Name (TEAMID)" ./package.sh 1.2.0
# also notarize + staple the dmg (profile saved once with: xcrun notarytool store-credentials)
SIGN_ID="Developer ID Application: Your Name (TEAMID)" NOTARY_PROFILE=ccwatch ./package.sh 1.2.0
```

With `SIGN_ID`, the app is signed with the hardened runtime and the Apple Events entitlement that click-to-focus and *Open full dashboard* need. The Developer ID and notarization path hasn't been tested yet, since only ad-hoc signing was available when this was written.

## Setup

```sh
python3 ccwatch.py --install --dry-run   # show what would change
python3 ccwatch.py --install             # do it
python3 ccwatch.py --doctor              # check everything
python3 ccwatch.py --uninstall           # undo it (also takes --dry-run)
```

`--install` does three things, and running it again changes nothing:

1. **Hook entries** in `~/.claude/settings.json` for the 12 events in the table under *Waiting hook* below, each running `ccwatch-hook.sh` in the background. Your other hooks are left alone. Earlier ccwatch entries (for example, ones pointing at an old path) are replaced. Run from inside a packaged app, it first copies the hook to `~/.claude/ccwatch-hook.sh`.
2. **Status line capture.** If your status line runs a script file with an `input=$(cat)` line, the capture block (marked `# ccwatch:`) goes right after it. With no status line at all, it creates `~/.claude/ccwatch-statusline.sh` (the model and context %, plus the capture) and uses that. An inline status line command can't be edited safely, so it prints the block for you to add.
3. **Backups** before the first edit of each file: `settings.json.bak-ccwatch` and `<status line script>.bak`. An existing backup is never overwritten, so it stays the copy from before ccwatch.

It warns if `jq` is missing, since the hook and status line need it. Running sessions may need a restart to pick up the hooks.

`--doctor` checks Python, `jq`, the session registry, transcripts, every hook entry and its script, the status line, how fresh the newest capture is, and leftover "needs you" flags. It prints ✓ / ✗ / ! with a hint for each problem, and exits non-zero when something is broken.

## Reading the dashboard

### Session cards

Each running session gets a card. The robot and colors show its state at a glance:

| State | Look | Meaning |
|---|---|---|
| **Needs you** | yellow, pulsing border, waving robot, `▲ NEEDS YOU` | Waiting on a permission prompt, a question, plan approval or an MCP input request. Always sorted first. |
| **Working** | green, typing robot, light running along the top border, `⠋ WORKING` | Claude is mid-turn. |
| **Idle** | blue/dim, sleeping robot with drifting `z` | Turn finished, waiting for your next prompt. |
| **Other** | yellow `!` | Claude Code reported a status ccwatch doesn't recognise; the raw status is shown in the badge. |

Card lines, top to bottom:

1. **Title**: the session's custom or auto-generated title (falls back to your last prompt).
2. **`▸ project`**: the working directory name.
3. **Activity**: `now ⚒ Tool` and its target while working, `last …` when idle, or `approve` / `answer` / `input` with the pending tool when it needs you.
4. **Timing**: how long it has been in the current state, total uptime, and the model.
5. **`io`**: sparkline of how fast the transcript is growing (about the last 20 seconds), plus `◆ active/total agents` if it has spawned sub-agents.
6. **`ctx`**: context-window fill (green < 50%, yellow < 80%, red ≥ 80%), session cost and lines added/removed.
7. **Sub-agents**: up to three active sub-agents, each with its own spinner and task description.

### Usage panel

| Row | What it shows |
|---|---|
| **5-hour / weekly** (or whatever windows your plan reports) | Plan limit used, as reported by Claude Code (the same numbers as `/usage`). The cyan `┃` marks how far through the window you are, so usage left of the marker means you're under pace. The text projects usage at the reset if you keep the current rate. |
| **today** | 24 slots, one per hour, of output tokens (current hour pulses, future hours are dots); then output tokens, tokens actually processed (input + cache writes + output), cache reads with hit rate, and number of replies. Counts every transcript today, sub-agents included. |
| **models** | Share of today's output tokens per model. |
| **cost** | Summed cost of every session that reported today, and of sessions open now. |

Pace messages and colors:

| Message | Color | When |
|---|---|---|
| `on pace for ~40% by reset` | green | projected < 70% |
| `on pace for ~90% by reset` | yellow | projected 70–99% |
| `▲ limit in ~51m at this pace` | red | projected to run out before the reset |
| `▲ limit reached` | red | 100% used |
| `window just started` / `no usage yet` | dim | too early to project |

Cache reads are shown separately because they usually make up 90%+ of raw token counts and would hide how much work was actually done.

### Projects today

Every directory Claude worked in today, across all its sessions and sub-agents, sorted by time spent. On wide terminals (about 145+ columns) it sits beside the usage panel; otherwise it's stacked below it, showing up to four projects (three and a `+N more` line when there are more). In compact mode (`u`) it folds into a single `worked` row of the usage panel.

| Column | What it shows |
|---|---|
| marker | Liveliest open session in the project: spinner = working, `▲` = needs you, `●` = open and idle, `·` = no open session |
| **time** | Time spent today (see below) |
| **0 … 18** | Time spent per hour of today; a full block is a fully active hour, dots are hours still to come, and the current hour pulses while a session there is working |
| **prompts** | Prompts you typed (tool results, slash-command output and sub-agent prompts aren't counted) |
| **lines** | Lines added / removed by Claude's Edit and Write tools |
| **files** | Files changed by Edit and Write |
| **commits** | `git commit` commands Claude ran that succeeded |
| **cost** | Summed cost of the project's sessions that reported today |
| **out** | Output tokens |
| **branch** | Git branch of the latest message |

Columns are dropped in this order as the panel narrows: branch, out, files, cost, commits, lines, prompts, then the hour strip. A `·` in a number column means zero.

The top border shows total time active today and when the first activity was; the bottom border totals projects, sessions, prompts, lines and commits.

**How time is counted:** every user and assistant message in the transcripts is a timestamp. A pause of up to 5 minutes (`IDLE_GAP`) between two messages counts as time spent, whether Claude was working or you were reading and typing; a longer pause counts as a break. Each project's time merges its parallel sessions, and the "active" total in the border merges all projects, so it can be less than the sum of the rows.

## How it works

ccwatch combines five local data sources, polled once a second (today's token totals and projects every five):

```
~/.claude/sessions/<pid>.json                 Claude Code's live session registry: name, cwd, busy/idle
~/.claude/projects/*/<session>.jsonl          transcript: current tool, title, model, token usage
~/.claude/projects/*/<session>/subagents/     sub-agent transcripts and their task descriptions
~/.cache/ccwatch/statusline/<session>.json    saved by the status line: plan limits, cost, context %
~/.cache/ccwatch/state/<session>.json         written by ccwatch-hook.sh while a session needs you
```

- **Live sessions**: a session file only counts if its process is still alive, so crashed sessions disappear on their own.
- **Tidying up**: once an hour, ccwatch deletes status line captures older than 8 days and "needs you" flags more than a day old whose session has gone. Both live in its own `~/.cache/ccwatch`.
- **Transcripts**: only the last 256 KB is re-read, and only when the file grows. Today's token totals and project stats come from one incremental scan of every transcript changed today, which picks up from where the last scan stopped. A session belongs to the directory it was in at its first message today.
- **Status line capture**: Claude Code passes the status line command a JSON blob that includes `rate_limits`, `cost` and `context_window`. That data isn't stored anywhere else, so the status line script saves it per session. Plan limits come from whichever session reported most recently.
- **Limit windows aren't hard-coded**: `rate_limits` gives each window as a key with `used_percentage` and `resets_at`, but no length. ccwatch reads the length from the key name, so if Anthropic changes the session window, it just follows:

  | Key | Label | Length |
  |---|---|---|
  | `five_hour` | 5-hour | 5 h |
  | `four_hour` | 4-hour | 4 h |
  | `seven_day` | weekly | 7 days |
  | `seven_day_opus` | weekly opus | 7 days |

  Every window in the blob is shown, shortest first, in the dashboard and the widget. In the menu bar icon, the shortest window drives the bar, and the longest (the plain one over a per-model one) colours the robot. A key it can't parse still shows its percentage, but with no pace projection or elapsed marker.
- **Waiting hook**: `ccwatch-hook.sh` is registered on these events. It prints nothing and always exits 0, so it can't block or alter a permission decision. It runs in the background (`async`), so it adds no delay to tool calls.

  | Sets "needs you" | Clears it |
  |---|---|
  | `PermissionRequest` | `PostToolUse`, `PostToolUseFailure` |
  | `PreToolUse` for `AskUserQuestion` / `ExitPlanMode` | `PermissionDenied`, `ElicitationResult` |
  | `Elicitation` | `UserPromptSubmit` |
  | `Notification` (`permission_prompt`, `elicitation_dialog`) | `Stop`, `StopFailure`, `SessionEnd` |

## Limitations

- **Undocumented internals.** `~/.claude/sessions/*.json` and the transcript format aren't a public API. A Claude Code update could change them; if cards go blank, check those files first.
- **Approving a prompt.** No hook fires at the moment you click approve, so a long-running command keeps showing "needs you" until it finishes.
- **Denying a prompt.** No hook fires for a manual denial either. The dashboard drops the flag once the session writes new transcript output or goes idle, usually within a few seconds.
- **Plan limits need a status line refresh.** They update when any session's status line refreshes. With no sessions open, the panel shows the last known values with an "as of … ago" note.
- **Pace projection is linear.** It assumes your usage rate so far continues, and treats each window as starting exactly one window length before its reset time.
- **Cost is an estimate.** It's Claude Code's API-equivalent figure; subscriptions aren't billed per token. "Today" sums each session's running total, so a session that started before midnight is counted in full.
- **Sub-agent activity is inferred.** A sub-agent counts as active if its transcript changed in the last 30 seconds.
- **Project time is an estimate.** It's built from message timestamps, so a single tool call that runs longer than 5 minutes with no messages in between is counted as a break.
- **Project stats only see Claude's own tools.** Lines and files count Edit and Write changes, not files changed by shell commands or by you. Commits are found by matching `git commit` at the start of a Bash command; ones run through scripts or aliases aren't counted.
- **Projects follow the starting directory.** A session that `cd`s elsewhere stays under the directory it was in at its first message today. Project cost has the same caveat as total cost: a session that started before midnight counts in full.

## Configuration

Tunables are constants at the top of `ccwatch.py`:

| Constant | Default | Meaning |
|---|---|---|
| `FPS` | `12` | Animation frame rate |
| `DATA_INTERVAL` | `1.0` | Seconds between session polls |
| `LEDGER_INTERVAL` | `5.0` | Seconds between scans for today's token totals and projects |
| `IDLE_GAP` | `300` | Longest pause, in seconds, that still counts as time spent on a project |
| `PROJECTS_STACKED` | `5` | Lines of the projects panel when it's stacked below usage |
| `SUB_ACTIVE_SECS` | `30` | How recently a sub-agent must have written to count as active |
| `MAX_CARD_W` | `80` | Maximum card width on wide terminals |
| `PRUNE_INTERVAL` | `3600` | Seconds between clean-ups of `~/.cache/ccwatch` |
| `CAPTURE_MAX_AGE` | `8 days` | Status line captures older than this are deleted |
| `STATE_MAX_AGE` | `1 day` | "Needs you" flags of ended sessions older than this are deleted |

Widget settings live in its defaults (`defaults read local.ccwatch.widget`). Most are set from the right-click menu; `notifyAfter` (seconds, default 120) is set with `defaults write`.

## Troubleshooting

Start with `python3 ccwatch.py --doctor` (or *Run Diagnostics…* in the widget's menu). It checks most of the rows below.

| Symptom | Check |
|---|---|
| "No Claude sessions running" while sessions are open | `ls ~/.claude/sessions/`. If it's empty, this Claude Code version may not write the registry. |
| `ctx (waiting for status line)` on a card | That session hasn't refreshed its status line since the capture was added; it fills in after its next reply. |
| Limits panel says "waiting for a status line update" | `ls ~/.cache/ccwatch/statusline/`. If it's empty, confirm `statusLine.command` in `~/.claude/settings.json` points at `statusline-command.sh` and that `jq` is installed. |
| "Needs you" never appears | Run `/hooks` in Claude Code to confirm the `ccwatch-hook.sh` entries are loaded, and check `ls ~/.cache/ccwatch/state/` while a permission prompt is open. |
| "Needs you" stays after you've answered | Give it a few seconds. If it persists, `rm ~/.cache/ccwatch/state/<session>.json` clears it. |
| Clicking a session only brings the app forward | The exact tab or window wasn't found, or permission was declined. Run `python3 ccwatch.py --focus <pid>` to see what it found. Allow ccwidget under System Settings → Privacy & Security → Automation (and Accessibility for JetBrains IDEs). |
| Widget shows a yellow error line | That's the last error from `ccwatch.py --json`. Run `python3 ccwatch.py --json --once` in this folder to see it in full. |
| No notifications | Check the *Notifications* submenu, then System Settings → Notifications → ccwidget. An ad-hoc signed build may need the permission granted again after a rebuild. |
| ⌃⌥⌘J / ⌃⌥⌘W do nothing | *Global Shortcuts* must be ticked in the menu. Another app may already hold the shortcut. |
| Floating panel is gone | It may be hidden: right-click the menu bar icon and tick *Show floating widget*. |
| Hovering assistant never appears | Check that *Show hovering assistant* is ticked in the right-click menu, and that the session shows as needing you in the widget (see *"Needs you" never appears*). If you closed it with ×, it stays away until a different session needs you. |
| Menu bar icon is missing | The menu bar is full, so it sits behind the notch. Quit or hide a few other menu bar apps, or ⌘-drag icons to make room. |
| Packaged app says "ccwatch.py not found inside or next to ccwidget.app" | The app was built with `build-widget.sh`, not `package.sh`, and then moved away from this folder. Use the app from `dist/`, or rebuild with `./package.sh`. |
| Packaged app shows "couldn't start python3" | No Python 3 at `/opt/homebrew/bin`, `/usr/local/bin` or `/usr/bin`. Install it (Homebrew, python.org, or the Command Line Tools via `xcode-select --install`), then reopen the app. |
| "ccwidget" Not Opened, "Apple could not verify…" on another Mac | The app is ad-hoc signed and was downloaded or received, so it's quarantined. Click **Done**, then System Settings → Privacy & Security → **Open Anyway**, or run `xattr -dr com.apple.quarantine /Applications/ccwidget.app`. See `Open Me First.txt` and *Signing* under Packaging. |
| Floating panel is off-screen after unplugging a display | `defaults delete local.ccwatch.widget` then reopen it; the panel starts in the top-right corner. (The assistant always appears on a connected display.) |

To test the hook by hand:

```sh
printf '%s' '{"session_id":"test","hook_event_name":"PermissionRequest","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | ./ccwatch-hook.sh && cat ~/.cache/ccwatch/state/test.json
rm ~/.cache/ccwatch/state/test.json
```

## Uninstall

1. Run `python3 ccwatch.py --uninstall` (`--dry-run` first to preview). It removes the hook entries whose command contains `ccwatch-hook.sh` and the `# ccwatch:` block from your status line script, or the whole `ccwatch-statusline.sh` status line if `--install` created it. Restoring the backups (`~/.claude/settings.json.bak-ccwatch`, `<status line script>.bak`) also works, but only if you haven't changed those files since.
2. Delete `~/.claude/ccwatch-hook.sh` if `--install` copied it there from a packaged app.
3. Delete the saved data: `rm -rf ~/.cache/ccwatch`.
4. If you used the widget: quit it, remove it from Login Items, delete `ccwidget.app` (and `/Applications/ccwidget.app` if you installed a packaged copy), and run `defaults delete local.ccwatch.widget` to forget its position and settings.

## License

[MIT](LICENSE) © 2026 Mark Ramos
