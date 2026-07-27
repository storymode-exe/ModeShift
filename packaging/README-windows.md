# ModeShift on Windows (portable)

## Using the portable build

1. Unzip `ModeShift-windows-portable.zip` anywhere you like, for example
   `C:\Tools\ModeShift`. Avoid `Program Files`: the folder needs to be
   writable, since your configuration lives beside the executables.
2. Install and start OpenRGB, then turn on its SDK server
   (Settings, SDK Server, Start Server). ModeShift talks to that.
3. Run `ModeShift.exe` to set up profiles.
4. In the Settings tab, tick "Start the watcher when I log in" if you want the
   tray daemon running from boot. That writes a single registry Run entry under
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, and unticking removes
   it. Nothing else touches the system.

Everything the app writes stays in the folder you unzipped:

    modeshift.json          profiles, modes, zones, key states
    settings.json           editor preferences
    custom_colors.json      your saved colour slots
    watcher_command.json    how the editor talks to the running watcher
    watcher.pid             which watcher is live

To uninstall, untick autostart and delete the folder.

## Notes

- The tray icon shows the active profile's colour on one half of the mark, so
  you can tell at a glance which profile is applied.
- Key capture uses `pynput`, which needs no special permissions on Windows.
  Some anti-cheat software objects to global key hooks; if a game refuses to
  launch with the watcher running, quit the watcher from its tray menu.
- The first launch after unzipping can take a few seconds while Windows scans
  the new executables.

## Building it yourself

From a checkout, on Windows, with Python 3.10 or newer on PATH:

    packaging\build.bat

That installs PyInstaller and the runtime dependencies, builds
`dist\ModeShift\`, and zips it. PyInstaller cannot cross compile, so the build
has to run on Windows.
