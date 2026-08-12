' Starts the service with no console window.
' Put a shortcut to this file in shell:startup to have it run at login.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
' 0 = hidden window, False = do not wait for it to exit
shell.Run """" & here & "\start.bat""", 0, False
