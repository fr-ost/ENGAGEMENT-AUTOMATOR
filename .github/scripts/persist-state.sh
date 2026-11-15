#!/usr/bin/env bash
# Commits the bot's data/ and reports/ and pushes them to the branch it runs on.
# Called by the bot after every cycle, and once more by the workflow as a safety net.
#
# Exit codes:
#    0  saved (or there was nothing to save)
#   10  saved, and files other than the bot's own data have changed on the branch
#       since this run started (START_SHA): the run should hand over to a fresh one
#    1  could not save
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../x-daily-automation" || exit 1
branch="${BRANCH:?BRANCH is required}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for dir in data reports; do
  if [ -d "$dir" ]; then git add --all -- "$dir"; fi
done
if ! git diff --cached --quiet; then
  git commit --quiet -m "chore(bot): state and report $(TZ=Asia/Dhaka date '+%F %H:%M')" || exit 1
fi

saved=0
for attempt in 1 2 3 4; do
  if git fetch --quiet origin "$branch"; then
    if [ -z "$(git rev-list --max-count=1 "origin/${branch}..HEAD")" ]; then
      saved=1 # nothing left to push
      break
    fi
    if ! git merge-base --is-ancestor "origin/${branch}" HEAD; then
      echo "Branch ${branch} moved while the bot was running; rebasing the bot's commits onto it."
      if ! git rebase --quiet "origin/${branch}"; then
        git rebase --abort
        echo "::error::The bot's state conflicts with newer commits on ${branch}. Nothing was force-pushed. Resolve manual edits to x-daily-automation/data if this repeats."
        exit 1
      fi
    fi
    if git push --quiet origin "HEAD:${branch}"; then
      saved=1
      break
    fi
  fi
  sleep $((attempt * 5))
done
if [ "$saved" != 1 ]; then
  echo "::error::Could not push the bot's state to ${branch}."
  exit 1
fi

if [ -n "${START_SHA:-}" ] && git cat-file -e "${START_SHA}^{commit}" 2>/dev/null; then
  if ! git diff --quiet "$START_SHA" "origin/${branch}" -- \
    ":/" ":(top,exclude)x-daily-automation/data" ":(top,exclude)x-daily-automation/reports"; then
    echo "The bot's code or workflow changed on ${branch} since this run started."
    exit 10
  fi
fi
exit 0
