Set fso = CreateObject("Scripting.FileSystemObject")
scriptPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "Start-AtlasDataServer-OffloadDisabled.ps1")

If Not fso.FileExists(scriptPath) Then
    MsgBox "Can't find " & scriptPath, vbExclamation, "ADS Offload Disabler"
    WScript.Quit 1
End If

CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptPath & """", 0, False
