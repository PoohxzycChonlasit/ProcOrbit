# ProcOrbit

## Intelligent Process Monitor & Control

ProcOrbit is an intelligent process monitor and control application. Its goal
is to make local processes easier to understand, with clear resource history,
safe user-controlled actions, quick access from a global widget, and useful
security visibility without requiring an AI service.

## Development direction

ProcOrbit is being developed Windows-first. macOS support may follow after the
process, startup, widget, and packaging boundaries are designed and validated
for that platform.

## Current implementation status

The source currently includes Windows-first process enumeration, PID plus
creation-time identity, conservative process categories, an incremental Qt
process table, search, sorting, and category filtering. Universal process
snapshots stay in memory. The existing Python workload monitor and its local
history remain part of the source; private Python metadata persistence is
opt-in.

Unit and Qt integration tests cover the collector-to-window update path using
the Qt event loop and controlled child processes. These tests run with Qt's
offscreen platform. Native desktop visual acceptance and an installer have not
been verified or released.

The following capabilities are **planned** and are not implemented product
features:

- **Universal resource history:** durable history for the universal process
  inventory; current persisted history covers Python workloads only.
- **Global widget:** quick access to process status and controls from a desktop
  widget or global launcher.
- **Safe process management:** explicit confirmation and identity checks before
  stopping a process.
- **Security visibility:** carefully scoped visibility into network activity,
  ports, and other relevant process signals.

## Development and licensing

The repository is public and the application source is under active
development. No project license has been selected, so source availability does
not grant permission to reuse, modify, or redistribute ProcOrbit. It is not yet
a licensed open-source software release.

Runtime dependencies are declared in `requirements.txt` and are not bundled in
this repository. Source publication is separate from binary distribution;
Qt/PySide6 notices and redistribution requirements must be reviewed before a
binary package or installer is created.

## Development checks

Run the tests from an environment that already has the declared dependencies:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -B -m unittest discover -s tests -v
```

The tests use an isolated temporary ProcOrbit runtime. The interactive
application uses the current Windows user's local application data directory.
