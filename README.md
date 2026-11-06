# ProcOrbit

## Intelligent Process Monitor & Control

ProcOrbit aims to make running processes easier to understand and manage. The
long-term product vision is a local-first desktop monitor with clear resource
history, safe user-controlled actions, quick access from a global widget, and
useful security visibility without requiring an AI service.

## Development direction

ProcOrbit is being developed Windows-first. macOS support may follow after the
process, startup, widget, and packaging boundaries are designed and validated
for that platform.

The following capabilities are **planned** and are not released ProcOrbit
features:

- **Process monitoring:** a searchable view of processes, resource use,
  relationships, and explainable categories.
- **Global widget:** quick access to process status and controls from a desktop
  widget or global launcher.
- **Safe process management:** explicit confirmation and identity checks before
  stopping a process.
- **Security visibility:** carefully scoped visibility into network activity,
  ports, and other relevant process signals.

## Current status

This public repository is an initial documentation-only bootstrap. It does not
yet contain a ProcOrbit application or released product features.

A separate local PythonWatch baseline currently provides Python-workload
monitoring, CPU and memory readings, process trees, local SQLite history, and a
system-tray UI. That code has not yet been included or rebranded here. Its
existing behavior is not evidence that any planned ProcOrbit feature is
implemented or verified.

## Open-source development

The repository is public and in early development. A project license has not
been selected, and no application source or release has been published here.
Until licensing and third-party notices are reviewed, ProcOrbit is not yet a
licensed open-source software release.