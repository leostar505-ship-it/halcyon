# Halcyon

A voice assistant for Windows that uses Claude Code as its brain. Say "hal-see-on" or press Ctrl+Alt+H. Runs locally: speech recognition, voice, HUD.

## Setup

1. Install Claude Code and sign in (`claude` works in a terminal).
2. Download `Halcyon-Setup.exe` from Releases and run it. Tick "Start Halcyon when Windows starts" if you want it to auto run. It lives in the tray, and right click it for Settings. The speech engine is included; the speech model downloads on first run.
3. Passwordless SSH to the server: `ssh leo@192.168.8.50` must work without a prompt. If it does not, Halcyon puts the fix on your clipboard and shows the steps.
4. Optional voice: run `tools/chatterbox_server.py` (needs `pip install chatterbox-tts`). Without it Halcyon uses the Windows voice.
5. Close Display Companion before starting. Both use the small screen's serial port.

## Jellyfin

Install the Webhook plugin, add a Generic destination with URL `http://<pc-ip>:8765/jellyfin` (add `?token=...` if you set `webhook_token`), tick Authentication Success, Authentication Failure, Playback Start and Session Start, and use this template:

```
{"event":"{{NotificationType}}","user":"{{NotificationUsername}}","device":"{{DeviceName}}","ip":"{{RemoteEndPoint}}","client":"{{ClientName}}","item":"{{ItemName}}"}
```

Repeated events from the same user are silenced for two minutes. Five failed logins in a row escalate to red.

## Safety

- Anything that deletes, overwrites, installs, changes system settings or sends data outside your network pauses and asks yes or no, by voice or from the tray. Silence is no.
- Spending money is blocked outright. Saying yes does not override it, and actions that look like they pay for things will not load.

## Actions

Drop a Python file in `%APPDATA%\Halcyon\actions` with `NAME`, `DESCRIPTION` and `run(ctx, args)`. It is live straight away. Every change is a git commit in that folder, so `git revert` rolls it back.

## Settings

Tray, Settings: hotkey, wake words, speech models, GPU, Claude models, server, webhook, small screen, start with Windows. Everything is stored in `%APPDATA%\Halcyon\config.json`.
