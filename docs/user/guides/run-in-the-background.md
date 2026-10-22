# Running the daemon in the background

The daemon is an ordinary Python program. It runs until you stop it, and it does
not install itself as a service. This page says how to have it start on its own,
and which computer to run it on.

## Which computer

| Where | Good for | Watch out for |
| --- | --- | --- |
| **An always-on machine**: a server, a NAS or a Raspberry Pi, in [Docker](../getting-started/install-with-docker.md) | The Presto: it needs the daemon to be there whenever it polls | Claude's local activity logs, the Claude usage cache and Codex cannot be read from a container. Claude's quota can still arrive through a [remote collector](remote-collectors.md) |
| **Your own PC** | Everything, including Codex and the local Claude readers, which read your user profile | When the PC sleeps or shuts down, the Presto shows **Daemon offline** |

A remote collector is different: it is not a service and there is nothing to run
in the background. Claude Code starts it. You need a collector only when Claude
Code runs on a different computer from the daemon, and you do not need one on a
computer that runs both.

## Windows: a task that starts when you sign in

Run it as a scheduled task that starts at your logon, as you. **Do not make it a
Windows service.** Three things only your own account can reach: the encryption
key file (Windows protects it for your account, so no other account can unlock
it), the Claude and Codex readers (they read your profile folder), and the
Codex sign-in. A service running as SYSTEM or any other account would start
without any of them.

In a normal PowerShell window (not an administrator one), change the three paths
to match your install:

```powershell
$repo   = "D:\github\presto-ai-dash"        # where the project is
$config = "$repo\dash.toml"                 # your configuration file
$python = "$repo\.venv\Scripts\python.exe"  # the Python you installed it into

$action = New-ScheduledTaskAction -Execute $python `
  -Argument "-m presto_ai_dash.app --config `"$config`" --admin" `
  -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Presto AI Dash" -Action $action `
  -Trigger $trigger -Settings $settings
```

What this does:

- It runs for the account that created it, only while you are signed in, with
  ordinary rights.
- It restarts up to three times, a minute apart, if the daemon exits with an
  error, and it is never stopped for running too long.
- **`-WorkingDirectory` matters.** `database = "dash.db"` is relative to where
  the daemon starts. A scheduled task starts in a Windows system folder by
  default, so without it the database and its key would be created there, not
  next to your configuration. You can also give `daemon.database` as a full path.
- A console window opens at each sign-in and shows the daemon's log. Minimise it.
  **Closing it stops the daemon.**

Start it now, and manage it:

```powershell
Start-ScheduledTask   -TaskName "Presto AI Dash"     # start it now
Stop-ScheduledTask    -TaskName "Presto AI Dash"     # stop it, the whole process
Get-ScheduledTask     -TaskName "Presto AI Dash" | Get-ScheduledTaskInfo
Unregister-ScheduledTask -TaskName "Presto AI Dash" -Confirm:$false   # remove it
```

Run one daemon at a time. The listeners bind exclusively, so a second one
refuses to start. Stop the task before you run the daemon by hand, and after you
edit `dash.toml`, stop it and start it again.

The first time it listens, Windows may ask whether to allow it through the
firewall. [Securing a deployment](securing-a-deployment.md) says how to allow
only the Presto's address.

### Hiding the window

`pythonw.exe` instead of `python.exe` gives no window, and **Stop** still ends it.
It also gives the daemon nowhere to write its log. The Problems list on the admin
page still collects warnings and errors, but a startup message such as a
temporary token is lost. If you use `pythonw.exe`, set explicit tokens in
`dash.toml` first so nothing you need is only in the log. Wrapping the command in
a hidden PowerShell to capture the log is not recommended: stopping the task then
ends the wrapper and leaves the daemon running.

### If the Presto says the daemon is offline

An asleep PC is the most common reason. Check the power settings, or run the
daemon on an always-on machine instead. The
[troubleshooting page](../troubleshooting/daemon-offline.md) has the rest.

## Docker

The container restarts with the machine and has no window to close: see
[Install with Docker](../getting-started/install-with-docker.md).

## Linux and macOS

Use the system's own service manager (a systemd user unit on Linux, a launchd
agent on macOS) to run `python -m presto_ai_dash.app --config dash.toml --admin`.
The same rules apply: run it as the user who owns the files, set the working
directory, and restart it on failure. The project tests the Windows task and
Docker only, so there is no recipe to copy here.

## See also

- [Install the daemon](../getting-started/install-the-daemon.md)
- [Remote collectors](remote-collectors.md)
- [Securing a deployment](securing-a-deployment.md)
