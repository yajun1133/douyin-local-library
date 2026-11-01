' ===================================================================
'  Douyin Author Local Bridge -- silent launcher (NO console window)
'  Double-click to run the service in the background.
'  Stop it with stop-service.bat (or reboot).
' ===================================================================
Option Explicit

Dim fso, sh, base, py, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

py = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
cmd = """" & py & """ -u """ & base & "\bridge.py"""
sh.Run cmd, 0, False
