Set objShell = CreateObject("WScript.Shell")
scriptPath = "C:\Users\Oli\Documents\ADS Tools\Start-AtlasDataServer-OffloadDisabled.ps1"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptPath & """"
objShell.Run cmd, 0, False
