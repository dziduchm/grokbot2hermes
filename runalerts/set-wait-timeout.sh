#!/bin/bash
# G2C set-wait-timeout: point the clarify and approval wait timeout of the
# G2C profiles at one value.
#
# What it manages, per profile:
#   agent.clarify_timeout   the clarify question window (resolver:
#                           tools/clarify_gateway.py resolve_clarify_timeout:
#                           legacy clarify.timeout if set, else
#                           agent.clarify_timeout, else 90; <= 0 unlimited)
#   approvals.timeout       the approval card window (tools/approval_context.py
#                           _get_approval_timeout: default 300, clamped to
#                           agent.deadline.MAX_SAFE_TIMEOUT_S)
# It reads the profile's OWN raw config.yaml (~/.hermes/profiles/<p>/config.yaml)
# to record the prior values: the merged `config get` can print a seeded
# default for a key the user layer never set, so it cannot be the rollback
# source of truth. Writes always go through `hermes -p <p> config set|unset`
# (HERMES_BIN overridable for tests). This script never edits a config file
# itself and never runs against anything but the profiles you name.
#
# Restart: NONE needed. Both values are re-read at every waiter creation
# through the signature-cached config loader (citations in
# runalerts/README.md "Wait timeout"), so the next waiter after `config set`
# uses the new value; waiters created earlier keep the window they were born
# with.
#
# Valid range 60..86400 seconds: below 60 s phone taps cannot land in time,
# above 24 h make a deliberate edit instead of a one-number batch change.
#
# Exit codes: 0 ok, 1 runtime refusal/failure, 2 invalid input (nothing touched).
# bash 3.2 compatible (macOS ships 3.2): no bash-4 features; POSIX awk/sed only.

set -u

DEFAULT_PROFILES="coder,code-reviewer,imilk,researcher"
KEY_CLARIFY="agent.clarify_timeout"
KEY_APPROVALS="approvals.timeout"
KEY_LEGACY="clarify.timeout"
MIN_WAIT_TIMEOUT=60
MAX_WAIT_TIMEOUT=86400
STATE_KIND="g2c-wait-timeout-state"

HERMES_BIN="${HERMES_BIN:-hermes}"
PROFILE_ROOT="${G2C_PROFILE_ROOT:-$HOME/.hermes/profiles}"
STATE_DIR_DEFAULT="$HOME/.bfc-pipeline/run/g2c"

progname=$(basename "$0")
stamp=$(date +%Y%m%d-%H%M%S)

apply=0
also_legacy=0
wait_timeout="600"
profiles="$DEFAULT_PROFILES"
state_file=""
rollback_file=""

die() {
    # die <exit_code> <message>
    code=$1
    shift
    printf 'FAIL: %s\n' "$1" >&2
    exit "$code"
}

usage() {
    printf 'set-wait-timeout.sh: set the clarify/approval wait timeout of the G2C profiles.\n'
    printf '\n'
    printf 'Usage:\n'
    printf '  bash set-wait-timeout.sh [--wait-timeout SECONDS] [--profiles a,b,c]\n'
    printf '                           [--apply] [--state FILE] [--also-legacy]\n'
    printf '  bash set-wait-timeout.sh --rollback FILE\n'
    printf '\n'
    printf 'Options:\n'
    printf '  --wait-timeout SECONDS  the new window for BOTH keys (default 600).\n'
    printf '                          Valid: %s..%s (rejects empty, non-digits, 0,\n' "$MIN_WAIT_TIMEOUT" "$MAX_WAIT_TIMEOUT"
    printf '                          leading zeros, 16+ digits; exit 2, nothing touched).\n'
    printf '                          Below 60 s phone taps cannot land in time; above\n'
    printf '                          24 h make a deliberate edit instead.\n'
    printf '  --profiles a,b,c        bare profile ids, [a-z0-9-]+, no leading dash (default: %s)\n' "$DEFAULT_PROFILES"
    printf '  --apply                 run the config sets (default: DRY RUN, prints the\n'
    printf '                          exact commands and touches nothing)\n'
    printf '  --state FILE            JSONL state file for --apply (default:\n'
    printf '                          %s/wait-timeout-state-<stamp>.jsonl,\n' "$STATE_DIR_DEFAULT"
    printf '                          created 0600; records the prior raw-layer value of\n'
    printf '                          every key per profile, null = it was not set)\n'
    printf '  --also-legacy           a profile that still sets the legacy `%s`\n' "$KEY_LEGACY"
    printf '                          makes the script refuse (that key overrides %s\n' "$KEY_CLARIFY"
    printf '                          and would silently win). With --also-legacy the script\n'
    printf '                          proceeds AND points that legacy key at the same value\n'
    printf '                          for profiles where it already exists (its prior value\n'
    printf '                          is recorded too); unset legacy keys are left alone\n'
    printf '                          (%s governs them).\n' "$KEY_CLARIFY"
    printf '  --rollback FILE         restore the prior values recorded in FILE (config set\n'
    printf '                          for a recorded value, config unset for null; an\n'
    printf '                          "already unset" is tolerated and reported as ok).\n'
    printf '  HERMES_BIN              hermes binary to call (default: hermes)\n'
    printf '  G2C_PROFILE_ROOT        profile dir root (default: %s)\n' "$PROFILE_ROOT"
    printf '\n'
    printf 'Exit codes: 0 ok, 1 refusal/failure, 2 invalid input.\n'
    printf 'No gateway restart is needed (see runalerts/README.md "Wait timeout").\n'
}

# --- validation ---------------------------------------------------------------

# Fail-closed value guard, the kit's G2C_PRE_RESTART_LINES case shape: empty /
# non-digit / zero / leading zero / 16+ digits die here, before any arithmetic
# can see the string (on bash 3.2 a [ -lt ] test overflows around 20 digits).
validate_wait_timeout() {
    case "$1" in
        ''|*[!0-9]*|0|0[0-9]*|????????????????*)
            die 2 "--wait-timeout must be an integer in $MIN_WAIT_TIMEOUT..$MAX_WAIT_TIMEOUT seconds (got: '$1')"
            ;;
    esac
    if [ "$1" -lt "$MIN_WAIT_TIMEOUT" ] || [ "$1" -gt "$MAX_WAIT_TIMEOUT" ]; then
        die 2 "--wait-timeout must be in $MIN_WAIT_TIMEOUT..$MAX_WAIT_TIMEOUT seconds (got: $1)"
    fi
}

validate_profiles() {
    # Validates and rewrites $profiles into a cleaned comma list; dies on any
    # bad, dash-leading or duplicate name and on any empty item (a trailing
    # comma would otherwise be silently dropped).
    case "$profiles" in
        *,)
            die 2 "invalid profile name '' (empty item after the last comma in --profiles)"
            ;;
    esac
    cleaned=""
    rest="$profiles"
    while [ -n "$rest" ]; do
        name=${rest%%,*}
        next=${rest#*,}
        if [ "$next" = "$rest" ]; then
            rest=""
        else
            rest=$next
        fi
        case "$name" in
            -*)
                die 2 "profile name '$name' must not start with '-' (it could swallow the next option or reach hermes as a flag)"
                ;;
            ''|*[!a-z0-9-]*)
                die 2 "invalid profile name '$name' (allowed: [a-z0-9-]+)"
                ;;
        esac
        case " $cleaned " in
            *" $name "*)
                die 2 "duplicate profile in --profiles: $name"
                ;;
        esac
        if [ -n "$cleaned" ]; then
            cleaned="$cleaned,$name"
        else
            cleaned="$name"
        fi
    done
    [ -n "$cleaned" ] || die 2 "--profiles needs at least one profile"
    profiles="$cleaned"
}

# --- raw config.yaml reader (prior values and the legacy guard) ---------------

# Echoes a single token line:
#   A            key ABSENT from the raw user layer (or no config file)
#   V<number>    present, bare/quoted integer scalar <number>
#   E<reason>    cannot decide -> the caller must fail closed
# Only integer scalars at the direct-child indent of their top-level section
# count: the FIRST content line under the header fixes the child indent and
# the leaf must sit at exactly that indent. A leaf-shaped line at any other
# indent is a different path (agent.sub.clarify_timeout must never be reported
# as agent.clarify_timeout) and refuses; deeper content that never names the
# leaf cannot change the direct-child decision and is skipped (real installs
# nest maps under the target sections, e.g. agent.personalities). Flow style,
# empty-after-colon, non-numeric scalars, duplicate keys or duplicate section
# headers all fail closed instead of guessing. Alias/merge keys (<<, *, &)
# are not consulted: they keep a known, inherited blind spot.
raw_value_token() {
    p=$1
    sec=$2
    leaf=$3
    file="$PROFILE_ROOT/$p/config.yaml"
    if [ ! -f "$file" ]; then
        printf 'A\n'
        return 0
    fi
    awk -v sec="$sec" -v leaf="$leaf" '
        BEGIN { child_indent = -1 }
        {   line = $0
            sub(/\r$/, "", line)
            if (line ~ /^---( |$)/) { ins = 0; child_indent = -1; next }
            # Comments and blank lines carry no mapping content: skip them so
            # they can neither fix nor disturb the child indent, wherever they
            # sit (a column-0 comment must not end the section either).
            probe = line
            sub(/^[ \t]+/, "", probe)
            if (probe == "" || substr(probe, 1, 1) == "#") {
                next
            }
            if (line !~ /^[ \t]/ && line ~ /^[A-Za-z0-9_-]+[ \t]*:/) {
                hdr = line
                sub(/[ \t]*:.*/, "", hdr)
                child_indent = -1
                if (hdr == sec) {
                    nsec++
                    rem = line
                    idx = index(rem, ":")
                    rem = substr(rem, idx + 1)
                    gsub(/^[ \t]+/, "", rem)
                    gsub(/[ \t]+$/, "", rem)
                    if (rem == "" || rem ~ /^#/) {
                        ins = 1
                    } else if (substr(rem, 1, 1) == "{") {
                        if (index(rem, "}") == 0) {
                            print "E " sec ": multi-line flow section value is not supported"
                            bad = 1
                            exit 1
                        }
                        flow = substr(rem, 2, index(rem, "}") - 2)
                        if (index(flow, leaf) > 0) {
                            print "E " sec ": flow section value mentions " leaf "; cannot decide the prior value - fix the profile config.yaml by hand"
                            bad = 1
                            exit 1
                        }
                        ins = 0
                    } else {
                        print "E " sec ": unsupported section value (flow/inline/non-map)"
                        bad = 1
                        exit 1
                    }
                } else {
                    ins = 0
                }
                next
            }
            if (line !~ /^[ \t]/) {
                # Top-level content that is not a recognized section header
                # (dotted key, stray scalar): it ends the section block.
                ins = 0
                child_indent = -1
                next
            }
            # Enforce the documented depth: the FIRST content line under the
            # header fixes the direct-child indent; the leaf counts only at
            # exactly that indent. A leaf-shaped line at any other indent is a
            # different path and refuses (fail closed) rather than possibly
            # reporting the wrong key. Deeper content that does not mention
            # the leaf cannot change the direct-child decision - the child
            # exists iff a leaf-shaped line sits at the child indent - so it
            # is skipped (real installs nest maps under the sections).
            if (ins) {
                ind = 0
                if (match(line, /^[ \t]+/)) ind = RLENGTH
                if (child_indent == -1) child_indent = ind
                if (ind != child_indent && line ~ ("^[ \t]+" leaf "[ \t]*:")) {
                    print "E " sec "." leaf ": found at indent " ind " but the direct children of " sec " sit at indent " child_indent " - a nested " leaf " is not the " sec "." leaf " value; cannot decide, refusing fail closed"
                    bad = 1
                    exit 1
                }
            }
            if (ins && line ~ ("^[ \t]+" leaf "[ \t]*:")) {
                if (done) {
                    print "E " sec "." leaf ": duplicate key in " sec
                    bad = 1
                    exit 1
                }
                rest = line
                sub(("^[ \t]+" leaf "[ \t]*:"), "", rest)
                gsub(/^[ \t]+/, "", rest)
                gsub(/[ \t]+$/, "", rest)
                if (rest == "") {
                    print "E " sec "." leaf ": nothing after the colon (null or nested block)"
                    bad = 1
                    exit 1
                }
                q = substr(rest, 1, 1)
                if (q == "\"" || q == "\047") {
                    inner = substr(rest, 2)
                    cq = index(inner, q)
                    if (cq <= 1) {
                        print "E " sec "." leaf ": unsupported quoted scalar"
                        bad = 1
                        exit 1
                    }
                    val = substr(inner, 1, cq - 1)
                    tail = substr(inner, cq + 1)
                    ci = index(tail, "#")
                    if (ci > 0) tail = substr(tail, 1, ci - 1)
                    gsub(/[ \t]+$/, "", tail)
                    if (tail != "") {
                        print "E " sec "." leaf ": unsupported trailing text after the quoted value"
                        bad = 1
                        exit 1
                    }
                } else {
                    ci = index(rest, "#")
                    if (ci > 0) rest = substr(rest, 1, ci - 1)
                    val = rest
                    gsub(/[ \t]+$/, "", val)
                }
                if (val !~ /^-?[0-9]+$/) {
                    print "E " sec "." leaf ": prior value is not an integer scalar (got: " val ")"
                    bad = 1
                    exit 1
                }
                done = 1
                pending = val
            }
        }
        END {
            if (bad) exit 1
            if (nsec > 1) {
                print "E " sec ": duplicate section header"
                exit 1
            }
            if (done) print "V" pending
            else print "A"
        }
    ' "$file"
}

# decode_raw_token <token> -> echoes the prior value (empty = unset); exit 1 on
# a token shape that should never occur.
decode_raw_token() {
    case "$1" in
        A)
            return 0
            ;;
        V-*)
            v=${1#V-}
            case "$v" in
                ''|*[!0-9]*) return 1 ;;
            esac
            printf '-%s' "$v"
            return 0
            ;;
        V*)
            v=${1#V}
            case "$v" in
                ''|*[!0-9]*) return 1 ;;
            esac
            printf '%s' "$v"
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# read_prior <profile> <label> <key> -> echoes the prior value (empty = unset);
# exits 1 with a FAIL line when the raw layer cannot be decided.
read_prior() {
    p=$1
    label=$2
    key=$3
    sec=${key%%.*}
    leaf=${key#*.}
    tok=$(raw_value_token "$p" "$sec" "$leaf") || {
        printf 'FAIL: cannot record the prior %s of profile %s (raw reader said: %s); refusing fail closed - fix the profile config.yaml by hand or run --apply per profile\n' "$label" "$p" "$tok" >&2
        exit 1
    }
    case "$tok" in
        E*)
            printf 'FAIL: cannot record the prior %s of profile %s (raw reader said: %s); refusing fail closed - fix the profile config.yaml by hand or run --apply per profile\n' "$label" "$p" "$tok" >&2
            exit 1
            ;;
    esac
    v=$(decode_raw_token "$tok") || {
        printf 'FAIL: cannot record the prior %s of profile %s (raw reader returned a broken token: %s); refusing fail closed\n' "$label" "$p" "$tok" >&2
        exit 1
    }
    printf '%s' "$v"
}

# --- hermes call wrappers -----------------------------------------------------

# Each write wrapper echoes the exact command first, so even --apply keeps a
# transcript a human can replay by hand.
hermes_config_set() {
    printf '  %s -p %s config set %s %s\n' "$HERMES_BIN" "$1" "$2" "$3"
    "$HERMES_BIN" -p "$1" config set "$2" "$3"
}

hermes_config_unset() {
    printf '  %s -p %s config unset %s\n' "$HERMES_BIN" "$1" "$2"
    "$HERMES_BIN" -p "$1" config unset "$2"
}

hermes_config_get() {
    "$HERMES_BIN" -p "$1" config get "$2" 2>/dev/null
}

verify_get() {
    # verify_get <profile> <key> <expected>
    got=$(hermes_config_get "$1" "$2")
    if [ "$got" != "$3" ]; then
        die 1 "verify failed for profile $1 $2: config get printed '$got', expected '$3' - the state file has the prior values, roll back or fix by hand"
    fi
}

# --- apply --------------------------------------------------------------------

cmd_apply() {
    # L2: a typo in --profiles would make hermes config set CREATE a junk
    # profile dir on the first call; refuse the whole batch up front (before
    # anything is recorded or set) when a named profile dir is missing.
    missing=""
    IFS_backup=$IFS
    IFS=,
    for p in $profiles; do
        IFS=$IFS_backup
        if [ ! -d "$PROFILE_ROOT/$p" ]; then
            missing="$missing $p"
        fi
    done
    if [ -n "$missing" ]; then
        die 1 "profile dir(s) missing under $PROFILE_ROOT:$missing (hermes config set would create a junk profile for a typo) - create the profile dir(s) first or fix --profiles"
    fi
    # Pass 1: read every prior BEFORE touching anything; build the state file
    # content in a temp file; refuse the whole batch on any legacy key (unless
    # --also-legacy) or on any undecidable raw value.
    tmpdir="${TMPDIR:-/tmp}"
    tmp_state="${tmpdir}/.set-wait-timeout-state.$$"
    tmp_check="$tmp_state.ok"
    cleanup() {
        rm -f "$tmp_state" "$tmp_check"
    }
    trap cleanup EXIT
    # N4: the pre-copy temp holds the same content as the state file; write it
    # private (umask 077) instead of with the ambient default, then restore.
    umask_was=$(umask)
    umask 077
    printf '{"kind": "%s", "created_utc": "%s", "wait_timeout": %s}\n' "$STATE_KIND" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$wait_timeout" > "$tmp_state" || die 1 "cannot write the temp state file $tmp_state"
    umask "$umask_was"
    legacy_offenders=0
    IFS_backup=$IFS
    IFS=,
    for p in $profiles; do
        IFS=$IFS_backup
        c=$(read_prior "$p" "$KEY_CLARIFY" "$KEY_CLARIFY") || exit 1
        a=$(read_prior "$p" "$KEY_APPROVALS" "$KEY_APPROVALS") || exit 1
        l=$(read_prior "$p" "legacy $KEY_LEGACY" "$KEY_LEGACY") || exit 1
        line_c="null"
        line_a="null"
        line_l="null"
        [ -n "$c" ] && line_c="$c"
        [ -n "$a" ] && line_a="$a"
        [ -n "$l" ] && line_l="$l"
        printf '{"profile": "%s", "key": "%s", "prior": %s}\n' "$p" "$KEY_CLARIFY" "$line_c" >> "$tmp_state"
        printf '{"profile": "%s", "key": "%s", "prior": %s}\n' "$p" "$KEY_APPROVALS" "$line_a" >> "$tmp_state"
        if [ -n "$l" ]; then
            if [ "$also_legacy" -ne 1 ]; then
                printf 'FAIL: refusing to apply: profile %s still sets the legacy key %s=%s (it overrides %s, so the new value would be silently ignored there)\n' "$p" "$KEY_LEGACY" "$l" "$KEY_CLARIFY" >&2
                legacy_offenders=$((legacy_offenders + 1))
            else
                printf '{"profile": "%s", "key": "%s", "prior": %s}\n' "$p" "$KEY_LEGACY" "$line_l" >> "$tmp_state"
            fi
        fi
        printf '    %-14s %s=%s  %s=%s  legacy %s=%s\n' "$p" "$KEY_CLARIFY" "${c:-(unset)}" "$KEY_APPROVALS" "${a:-(unset)}" "$KEY_LEGACY" "${l:-(unset)}"
    done
    if [ "$legacy_offenders" -gt 0 ]; then
        printf 'FAIL: %s profile(s) with a legacy %s key; nothing was changed. Either remove the legacy key from those profiles by hand, or rerun with --also-legacy to point the legacy key at the same value (its prior value gets recorded and rolled back).\n' "$legacy_offenders" "$KEY_LEGACY" >&2
        exit 1
    fi
    if [ ! -s "$tmp_state" ]; then
        die 1 "internal: empty temp state file $tmp_state"
    fi
    # state file: complete before any mutation; never overwrite a record
    if [ -z "$state_file" ]; then
        state_file="$STATE_DIR_DEFAULT/wait-timeout-state-$stamp.jsonl"
    fi
    if [ -e "$state_file" ]; then
        die 1 "--state file already exists: $state_file (refusing to overwrite a rollback record)"
    fi
    state_dir=$(dirname "$state_file")
    if [ ! -d "$state_dir" ]; then
        mkdir -p "$state_dir" || die 1 "cannot create the state dir $state_dir"
    fi
    umask_was=$(umask)
    umask 077
    cp "$tmp_state" "$state_file" || die 1 "cannot write the state file $state_file"
    umask "$umask_was"
    chmod 600 "$state_file"
    cleanup
    trap - EXIT
    printf 'applying wait timeout %ss to profiles: %s\n' "$wait_timeout" "$profiles"
    printf 'state file: %s\n' "$state_file"
    printf 'rollback later: bash %s --rollback %s\n' "$progname" "$state_file"
    # Pass 2: apply + verify per profile; every set command is echoed first.
    IFS=,
    for p in $profiles; do
        IFS=$IFS_backup
        printf 'profile: %s\n' "$p"
        hermes_config_set "$p" "$KEY_CLARIFY" "$wait_timeout" || die 1 "config set $KEY_CLARIFY failed for profile $p (exit $?)"
        hermes_config_set "$p" "$KEY_APPROVALS" "$wait_timeout" || die 1 "config set $KEY_APPROVALS failed for profile $p (exit $?)"
        verify_get "$p" "$KEY_CLARIFY" "$wait_timeout"
        verify_get "$p" "$KEY_APPROVALS" "$wait_timeout"
        printf '  verify ok: %s=%s  %s=%s\n' "$KEY_CLARIFY" "$wait_timeout" "$KEY_APPROVALS" "$wait_timeout"
        if [ "$also_legacy" -eq 1 ] \
            && grep -q "\"profile\": \"$p\", \"key\": \"$KEY_LEGACY\"" "$state_file"; then
            hermes_config_set "$p" "$KEY_LEGACY" "$wait_timeout" || die 1 "config set $KEY_LEGACY failed for profile $p (exit $?)"
            verify_get "$p" "$KEY_LEGACY" "$wait_timeout"
            printf '  verify ok: %s=%s (legacy)\n' "$KEY_LEGACY" "$wait_timeout"
        fi
    done
    printf 'done: wait timeout %ss set and verified on: %s\n' "$wait_timeout" "$profiles"
    printf 'restart: none needed - the next waiter re-reads the profile config (signature-cached); waiters created before now keep their window. Citations in runalerts/README.md "Wait timeout".\n'
}

# --- rollback -----------------------------------------------------------------

parse_state_line() {
    # parse_state_line <line> -> echoes "profile|key|prior"; empty when the
    # line is not a restore record (header lines and blank lines are skipped
    # by the caller). Anything else-shaped than this exact record is a refusal
    # upstream: an incomplete rollback is worse than none.
    printf '%s\n' "$1" \
        | sed -n -E 's/^\{"profile": "([a-z0-9-]{1,64})", "key": "(agent\.clarify_timeout|approvals\.timeout|clarify\.timeout)", "prior": (null|-?[0-9]{1,15})\}$/\1|\2|\3/p'
}

cmd_rollback() {
    printf 'rollback from: %s\n' "$rollback_file"
    # N2: a state file whose last line has no newline was truncated mid-write;
    # the read loop would silently restore one record less than was recorded.
    # Refuse before touching anything.
    if [ -s "$rollback_file" ]; then
        lastbyte=$(tail -c 1 "$rollback_file")
        if [ -n "$lastbyte" ]; then
            die 1 "state file is incomplete: the last line does not end with a newline (truncated?): $rollback_file - refusing: an incomplete rollback is worse than none"
        fi
    fi
    ok=0
    lineno=0
    while IFS= read -r line; do
        lineno=$((lineno + 1))
        case "$line" in
            '')
                continue
                ;;
            '{'*'"kind": "'"$STATE_KIND"'"'*)
                continue
                ;;
        esac
        parsed=$(parse_state_line "$line")
        if [ -z "$parsed" ]; then
            die 1 "state file line $lineno is not understood: $line (refusing: an incomplete rollback is worse than none)"
        fi
        p=${parsed%%|*}
        rest=${parsed#*|}
        k=${rest%%|*}
        v=${rest#*|}
        if [ "$v" = "null" ]; then
            printf 'profile: %s  %s -> unset\n' "$p" "$k"
            if hermes_config_unset "$p" "$k"; then
                printf '  unset ok\n'
            else
                unset_rc=$?
                if [ "$unset_rc" -eq 1 ]; then
                    printf '  already unset (ok)\n'
                else
                    die 1 "config unset failed for profile $p key $k (exit $unset_rc) - inspect that profile by hand"
                fi
            fi
        else
            printf 'profile: %s  %s -> %s\n' "$p" "$k" "$v"
            hermes_config_set "$p" "$k" "$v" || die 1 "config set failed for profile $p key $k (exit $?)"
            verify_get "$p" "$k" "$v"
            printf '  restore ok: %s=%s\n' "$k" "$v"
        fi
        ok=$((ok + 1))
    done < "$rollback_file"
    if [ "$ok" -eq 0 ]; then
        die 1 "state file has no restore line(s): $rollback_file"
    fi
    printf 'rollback done: %s key(s) restored; waiters created while the temporary value was live keep that window.\n' "$ok"
}

# --- dry run ------------------------------------------------------------------

cmd_dry_run() {
    printf 'DRY RUN: nothing written, nothing run (use --apply to execute)\n'
    printf 'wait timeout: %ss\n' "$wait_timeout"
    printf 'profiles: %s\n' "$profiles"
    printf 'state file (on --apply): %s\n' \
        "${state_file:-$STATE_DIR_DEFAULT/wait-timeout-state-$stamp.jsonl}"
    printf '\nprior values (read from the raw profile config.yaml, not probed from hermes):\n'
    IFS_backup=$IFS
    IFS=,
    for p in $profiles; do
        IFS=$IFS_backup
        if [ ! -d "$PROFILE_ROOT/$p" ]; then
            printf '    %-14s profile dir missing: %s\n' "$p" "$PROFILE_ROOT/$p"
            continue
        fi
        c=$(read_prior "$p" "$KEY_CLARIFY" "$KEY_CLARIFY") || exit 1
        a=$(read_prior "$p" "$KEY_APPROVALS" "$KEY_APPROVALS") || exit 1
        l=$(read_prior "$p" "legacy $KEY_LEGACY" "$KEY_LEGACY") || exit 1
        printf '    %-14s %s=%s  %s=%s  legacy %s=%s\n' "$p" "$KEY_CLARIFY" "${c:-(unset)}" "$KEY_APPROVALS" "${a:-(unset)}" "$KEY_LEGACY" "${l:-(unset)}"
    done
    printf '\ncommands per profile:\n'
    IFS=,
    for p in $profiles; do
        IFS=$IFS_backup
        printf '  %s -p %s config set %s %s\n' "$HERMES_BIN" "$p" "$KEY_CLARIFY" "$wait_timeout"
        printf '  %s -p %s config set %s %s\n' "$HERMES_BIN" "$p" "$KEY_APPROVALS" "$wait_timeout"
        printf '  %s -p %s config get %s   # verify: prints %s\n' "$HERMES_BIN" "$p" "$KEY_CLARIFY" "$wait_timeout"
        printf '  %s -p %s config get %s    # verify: prints %s\n' "$HERMES_BIN" "$p" "$KEY_APPROVALS" "$wait_timeout"
    done
    printf '\nrestart: none needed - the gateway re-reads these values at every waiter creation (signature-cached config load, no process-level cache of the value); a waiter created before the change keeps its old window. Citations in runalerts/README.md "Wait timeout".\n'
}

# --- arg parsing --------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --apply)
            apply=1
            ;;
        --wait-timeout)
            shift
            [ $# -gt 0 ] || die 2 "--wait-timeout needs a value"
            wait_timeout=$1
            ;;
        --profiles)
            shift
            [ $# -gt 0 ] || die 2 "--profiles needs a value"
            profiles=$1
            ;;
        --state)
            shift
            [ $# -gt 0 ] || die 2 "--state needs a value"
            state_file=$1
            ;;
        --also-legacy)
            also_legacy=1
            ;;
        --rollback)
            shift
            [ $# -gt 0 ] || die 2 "--rollback needs a value"
            rollback_file=$1
            ;;
        *)
            printf 'FAIL: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

validate_wait_timeout "$wait_timeout"
validate_profiles

if [ -n "$rollback_file" ]; then
    if [ "$apply" -eq 1 ]; then
        die 2 "--apply and --rollback are mutually exclusive"
    fi
    if [ -n "$state_file" ]; then
        die 2 "--state and --rollback are mutually exclusive"
    fi
    if [ "$also_legacy" -eq 1 ] || [ "$wait_timeout" != "600" ]; then
        die 2 "--also-legacy and --wait-timeout do not apply to --rollback"
    fi
    if [ ! -f "$rollback_file" ]; then
        die 1 "state file not found: $rollback_file"
    fi
    firstline=$(head -n 1 "$rollback_file")
    printf '%s' "$firstline" | grep -qE "^\{\"kind\": \"$STATE_KIND\", \"created_utc\": \"[^\"]{1,40}\", \"wait_timeout\": [0-9]{1,5}\}$" \
        || die 1 "state file does not look like a $STATE_KIND record: $rollback_file"
    cmd_rollback
    exit 0
fi

if [ -n "$state_file" ] && [ "$apply" -eq 0 ]; then
    die 2 "--state only makes sense with --apply"
fi

if [ "$HERMES_BIN" = "hermes" ] && ! command -v hermes >/dev/null 2>&1; then
    if [ "$apply" -eq 1 ]; then
        die 2 "HERMES_BIN=hermes is not on PATH; set HERMES_BIN to the hermes launcher you want"
    fi
    printf 'note: HERMES_BIN=hermes is not on PATH (irrelevant for a dry run: the commands are printed, not run)\n'
fi

if [ "$apply" -eq 1 ]; then
    if [ "$HERMES_BIN" != "hermes" ] && [ ! -x "$HERMES_BIN" ]; then
        die 2 "HERMES_BIN=$HERMES_BIN is not executable"
    fi
    cmd_apply
    exit 0
fi

cmd_dry_run
exit 0
