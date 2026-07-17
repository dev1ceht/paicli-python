## Coding task completion

- For change, build, or fix requests, you must make the requested workspace changes. A description alone is not completion.
- Prefer `edit_file` or `apply_patch` for existing files. Use `write_file` for new files or an explicitly requested full overwrite.
- After editing, inspect the diff when available and run focused tests or checks that exercise the change.
- When a tool call fails, use its error as evidence and change your approach instead of repeating the same invalid call.
- Before the final response, verify that the requested modifications exist in the workspace and report the checks actually run.
