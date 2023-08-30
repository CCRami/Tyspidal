import winreg
import os
def add_to_startup():
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE); winreg.SetValueEx(key, 'Tyspidal', 0, winreg.REG_SZ,"C:\Program Files (x86)\Tyspidal\Tyspidal.exe")
            key.Close()
            print("Added to startup successfully.")
        except Exception as e:
            print("Error:", e)
def remove_from_startup():
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE); winreg.DeleteValue(key, 'Tyspidal')
        key.Close()
        print("Removed from startup successfully.")
    except Exception as e:
        print("Error:", e)
def startup(status):
    #print(os.getcwd())
    if status:
        add_to_startup()
    else:
        remove_from_startup()