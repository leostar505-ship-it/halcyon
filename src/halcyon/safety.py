import re

SPEND_RE = re.compile(
    r"\b(buy|buying|purchase|purchasing|checkout|check out|pay for|pay with|paying for|payment|subscribe|"
    r"subscription|donate|donation|top up|top-up|topup|add funds|credit card|debit card|paypal|stripe|apple pay|"
    r"google pay|spend money|spending money|place an order|order (it|one|some|a|me|online)|bid on|redeem)\b",
    re.I)

SPEND_CODE_RE = re.compile(r"(stripe|paypal|checkout|purchase|payment|credit.?card|add_to_cart|buy_now)", re.I)

REFUSAL = "I can't spend money, sir. You'll need to do that yourself."

CANCEL_RE = re.compile(r"^(never ?mind|cancel|forget it|stop|abort)\b", re.I)
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "affirmative", "proceed",
             "go", "do", "approve", "approved", "correct", "absolutely"}
NO_WORDS = {"no", "nope", "nah", "cancel", "stop", "negative", "don't", "dont", "deny", "denied", "never", "abort"}


def is_spend(text):
    return bool(SPEND_RE.search(text or ""))


def action_code_is_spend(source):
    return bool(SPEND_CODE_RE.search(source or ""))


def parse_yes_no(text):
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return None
    for w in words:
        if w in NO_WORDS:
            return False
    for w in words:
        if w in YES_WORDS:
            return True
    return None


def short(text, n=60):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "..."


def describe_tool(name, tool_input):
    tool_input = tool_input or {}
    if name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        first = cmd.split()[0].lower() if cmd.split() else ""
        verbs = {
            "scp": "copy a file", "rsync": "sync files", "ssh": "connect to the server",
            "rm": "delete files", "del": "delete files", "rmdir": "delete a folder", "remove-item": "delete files",
            "mv": "move files", "move": "move files", "move-item": "move files", "cp": "copy files",
            "copy": "copy files", "copy-item": "copy files", "pip": "install software", "winget": "install software",
            "choco": "install software", "npm": "install software", "apt": "install software",
            "reg": "change system settings", "set-itemproperty": "change system settings",
            "netsh": "change network settings", "sc": "change a service", "taskkill": "stop a program",
            "shutdown": "shut down", "format": "format a drive", "curl": "download something",
            "wget": "download something", "git": "run git",
        }
        verb = verbs.get(first, "run a command")
        return verb, short(cmd, 90)
    if name in ("Write", "Edit", "MultiEdit"):
        path = str(tool_input.get("file_path", ""))
        return "change a file", short(path, 90)
    return "use %s" % name, short(str(tool_input), 90)


def allow_pattern(name, tool_input):
    tool_input = tool_input or {}
    if name == "Bash":
        return "Bash(%s)" % str(tool_input.get("command", "")).strip()
    path = tool_input.get("file_path")
    if path:
        return "%s(%s)" % (name, path)
    return name


def denial_is_spend(name, tool_input):
    blob = "%s %s" % (name, tool_input)
    return bool(SPEND_RE.search(blob)) or bool(SPEND_CODE_RE.search(blob))
