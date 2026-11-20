import sys

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "Halcyon"


def command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    return '"%s" -m halcyon' % sys.executable


def is_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on):
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(k, VALUE)
                except OSError:
                    pass
        return True
    except OSError:
        return False
