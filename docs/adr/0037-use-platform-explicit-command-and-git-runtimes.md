# Use platform-explicit command and Git runtimes

PaiCLI exposes one production `execute_command` tool rather than a misleading `bash` alias. On Windows it invokes `powershell.exe -NoLogo -NoProfile -NonInteractive -Command` and identifies the runtime as Windows PowerShell 5.1; on POSIX it invokes `/bin/sh -lc`. The workspace is already the command working directory, and terminal input is disabled.
