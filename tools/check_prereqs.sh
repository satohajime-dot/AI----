#!/bin/sh
# check_prereqs.sh -- macOS / Linux prerequisite checker for the secretary method kit.
#
# WHY THIS IS NOT PYTHON: this script's whole job is to find out whether Python
# exists. It must run on a machine with nothing installed, so it uses only
# POSIX sh, which ships with every macOS and Linux.
#
# WHY THIS FILE IS ASCII-ONLY (no Japanese): it is the sibling of
# check_prereqs.ps1, which must be ASCII for PowerShell encoding reasons.
# The script emits machine readable ASCII; Claude explains it in Japanese.
#
# KEEP IN SYNC WITH check_prereqs.ps1 -- same keys, same verdicts, same
# required/optional split. A disagreement between the two means one platform
# gets a wrong answer.
#
# Usage:  sh tools/check_prereqs.sh
# Output: KEY=VALUE lines.
#   RESULT=ok            -> everything REQUIRED is present
#   RESULT=action_needed -> see MISSING (required) / OPTIONAL_MISSING (optional)
#
# REQUIRED: python, git      OPTIONAL: node (Google Chat only), gh (backup only)

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9
MIN_NODE=18

missing=""
optional_missing=""
blocked=""

add_missing() {
  if [ -z "$missing" ]; then missing="$1"; else missing="$missing,$1"; fi
}
add_optional_missing() {
  if [ -z "$optional_missing" ]; then optional_missing="$1"; else optional_missing="$optional_missing,$1"; fi
}
add_blocked() {
  if [ -z "$blocked" ]; then blocked="$1"; else blocked="$blocked,$1"; fi
}

# Extract the FIRST x.y or x.y.z from stdin.
# Do not do this with a sed capture -- the greedy `.*` eats the leading digit
# group and turns "Python 3.12.10" into "12.10". grep -o takes the first match
# and exists on both macOS (BSD grep) and Linux.
ver() {
  grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1
}

# Pre-release builds report a version that compares as stable (3.9.0rc1 -> 3.9.0)
# but are not what we want people running. CPython uses 3.13.0a1 / 3.13.0b2 /
# 3.13.0rc1, so the digit-letter-digit form has to be matched too -- not just
# the spelled-out words.
is_prerelease() {
  printf '%s' "$1" | grep -qiE '[0-9](a|b|rc)[0-9]+|alpha|beta|[-+]dev|nightly|canary'
}

# Run `<cmd> --version`, capture BOTH the output and the exit status.
# A version-looking substring is NOT proof of success: a broken install can
# still print text, and piping straight into ver() throws the exit status away.
# Sets: TOOL_RC, TOOL_RAW, TOOL_VER
probe() {
  TOOL_RAW=$("$1" --version 2>&1)
  TOOL_RC=$?
  if [ "$TOOL_RC" -eq 0 ]; then
    TOOL_VER=$(printf '%s' "$TOOL_RAW" | ver)
  else
    TOOL_VER=""
  fi
}

echo "PREREQ_CHECK_VERSION=2"
case "$(uname -s)" in
  Darwin) echo "OS=darwin" ;;
  Linux)  echo "OS=linux" ;;
  MINGW*|MSYS*|CYGWIN*)
    # A POSIX shell on Windows. What this shell sees is not what PowerShell and
    # Claude will see (different PATH, different python), so stop and redirect
    # rather than emit a misleading verdict.
    echo "OS=windows_shell"
    echo "RESULT=wrong_script"
    echo "HINT=run tools/check_prereqs.ps1 with PowerShell instead"
    exit 0
    ;;
  *)      echo "OS=unknown" ;;
esac
echo "OS_VERSION=$(uname -r)"

# ---- Python (REQUIRED) -----------------------------------------------------
python_cmd=""
python_ver=""
python_raw=""
python_broken=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    probe "$cand"
    if [ -n "$TOOL_VER" ]; then
      python_cmd="$cand"
      python_ver="$TOOL_VER"
      python_raw="$TOOL_RAW"
      break
    else
      python_broken="yes"
    fi
  fi
done

if [ -n "$python_cmd" ]; then
  echo "PYTHON_CMD=$python_cmd"
  echo "PYTHON_VERSION=$python_ver"
  if is_prerelease "$python_raw"; then
    echo "PYTHON=prerelease"
    add_missing python
  else
    pmaj=$(echo "$python_ver" | cut -d. -f1)
    pmin=$(echo "$python_ver" | cut -d. -f2)
    if [ "$pmaj" -gt "$MIN_PYTHON_MAJOR" ] 2>/dev/null; then
      echo "PYTHON=ok"
    elif [ "$pmaj" -eq "$MIN_PYTHON_MAJOR" ] && [ "$pmin" -ge "$MIN_PYTHON_MINOR" ] 2>/dev/null; then
      echo "PYTHON=ok"
    else
      echo "PYTHON=too_old"
      add_missing python
    fi
  fi
elif [ -n "$python_broken" ]; then
  echo "PYTHON=broken"
  add_missing python
else
  echo "PYTHON=missing"
  add_missing python
fi

# ---- git (REQUIRED) --------------------------------------------------------
# On macOS `git` may exist as an Xcode Command Line Tools shim that prompts a
# GUI install dialog on first use. Asking for the version reveals which it is.
if command -v git >/dev/null 2>&1; then
  probe git
  if [ -n "$TOOL_VER" ]; then
    echo "GIT=ok"
    echo "GIT_VERSION=$TOOL_VER"
    gn=$(git config --global user.name 2>/dev/null)
    ge=$(git config --global user.email 2>/dev/null)
    if [ -n "$gn" ] && [ -n "$ge" ]; then echo "GIT_IDENTITY=ok"; else echo "GIT_IDENTITY=unset"; fi
  else
    # macOS only: a `git` that exists but cannot report a version is the Xcode
    # Command Line Tools shim. Elsewhere it is simply a broken install --
    # report the same verdict the Windows checker would.
    if [ "$(uname -s)" = "Darwin" ]; then
      echo "GIT=shim_only"
    else
      echo "GIT=broken"
    fi
    add_missing git
  fi
else
  echo "GIT=missing"
  add_missing git
fi

# ---- Node.js (OPTIONAL -- Google Chat connection only) ---------------------
if command -v node >/dev/null 2>&1; then
  probe node
  if [ -n "$TOOL_VER" ]; then
    echo "NODE_VERSION=$TOOL_VER"
    if is_prerelease "$TOOL_RAW"; then
      echo "NODE=prerelease"
      add_optional_missing node
    else
      nmaj=$(echo "$TOOL_VER" | cut -d. -f1)
      if [ -n "$nmaj" ] && [ "$nmaj" -ge "$MIN_NODE" ] 2>/dev/null; then
        echo "NODE=ok"
      else
        echo "NODE=too_old"
        add_optional_missing node
      fi
    fi
  else
    echo "NODE=broken"
    add_optional_missing node
  fi
else
  echo "NODE=missing"
  add_optional_missing node
fi

# ---- GitHub CLI (OPTIONAL -- backup to GitHub only) ------------------------
if command -v gh >/dev/null 2>&1; then
  probe gh
  if [ -n "$TOOL_VER" ]; then
    echo "GH=ok"
    echo "GH_VERSION=$TOOL_VER"
    if gh auth status >/dev/null 2>&1; then echo "GH_AUTH=ok"; else echo "GH_AUTH=unset"; fi
  else
    echo "GH=broken"
    add_optional_missing gh
  fi
else
  echo "GH=missing"
  add_optional_missing gh
fi

# ---- package manager -------------------------------------------------------
if command -v brew >/dev/null 2>&1; then
  echo "PKGMGR=brew"
  probe brew
  if [ -n "$TOOL_VER" ]; then echo "PKGMGR_VERSION=$TOOL_VER"; fi
elif command -v apt-get >/dev/null 2>&1; then
  echo "PKGMGR=apt"
else
  echo "PKGMGR=none"
  add_blocked pkgmgr_missing
fi

# Xcode Command Line Tools are the prerequisite for Homebrew on macOS.
if [ "$(uname -s)" = "Darwin" ]; then
  if xcode-select -p >/dev/null 2>&1; then echo "XCODE_CLT=ok"; else echo "XCODE_CLT=missing"; fi
fi

# ---- rights ----------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then echo "ADMIN=true"; else echo "ADMIN=false"; fi
if command -v sudo >/dev/null 2>&1; then echo "SUDO=present"; else echo "SUDO=absent"; fi

# ---- summary ---------------------------------------------------------------
echo "MISSING=$missing"
echo "OPTIONAL_MISSING=$optional_missing"
echo "BLOCKED=$blocked"
if [ -z "$missing" ]; then echo "RESULT=ok"; else echo "RESULT=action_needed"; fi

# secretary-method-tool: check_prereqs v0.10
