#!/bin/sh
# ccwatch-hook.sh — records when a Claude Code session is blocked waiting on you.
#
# Wired to hook events in ~/.claude/settings.json. Writes
#   ~/.cache/ccwatch/state/<session_id>.json  {state, reason, tool, detail, since}
# when a session needs you, and deletes it when the session moves on.
# Prints nothing and always exits 0, so it never affects a permission decision.

dir="$HOME/.cache/ccwatch/state"
input=$(cat)

eval "$(printf '%s' "$input" | jq -r '
  def first_line: tostring | (split("\n")[0] // "") | .[0:160];
  (.tool_input | if type == "object" then .description // .command // .file_path // .questions[0].question // .plan // "" else "" end) as $d
  | @sh "sid=\(.session_id // "") ev=\(.hook_event_name // "") tool=\(.tool_name // "") ntype=\(.notification_type // "") msg=\(.message // "" | first_line) detail=\($d | first_line)"
' 2>/dev/null)" || exit 0
[ -n "$sid" ] || exit 0
f="$dir/$sid.json"

mark_waiting() {
  mkdir -p "$dir" &&
    jq -n --arg r "$1" --arg tool "$tool" --arg d "$2" --argjson ts "$(date +%s)" \
      '{state: "waiting", reason: $r, tool: $tool, detail: $d, since: $ts}' >"$f.tmp" &&
    mv "$f.tmp" "$f"
}

case "$ev" in
  PermissionRequest) mark_waiting permission "$detail" ;;
  PreToolUse) mark_waiting question "$detail" ;; # matcher limits this to AskUserQuestion|ExitPlanMode
  Elicitation) mark_waiting input "$msg" ;;
  Notification)
    case "$ntype" in
      permission_prompt) [ -f "$f" ] || mark_waiting permission "$msg" ;;
      elicitation_dialog) [ -f "$f" ] || mark_waiting input "$msg" ;;
    esac
    ;;
  *) rm -f "$f" ;; # PostToolUse, PostToolUseFailure, PermissionDenied, ElicitationResult, UserPromptSubmit, Stop, StopFailure, SessionEnd
esac
exit 0
