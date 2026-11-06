from __future__ import annotations

from typing import Any


_PRIVATE_HISTORY_FIELDS = (
    "ppid",
    "exe",
    "cmdline",
    "cwd",
    "script_path",
    "module_name",
    "project_path",
    "parent_process",
    "virtualenv_path",
    "exe_status",
    "cmdline_status",
    "cwd_status",
)


def history_metadata(
    metadata: dict[str, Any], *, include_private_details: bool = False
) -> dict[str, Any]:
    """Return Python history metadata under an explicit privacy policy."""
    result = dict(metadata)
    if include_private_details:
        return result
    for field in _PRIVATE_HISTORY_FIELDS:
        result[field] = None
    # Script and module labels can reveal project or task names too.
    if "script_label" in result:
        result["script_label"] = "Python workload"
    return result
