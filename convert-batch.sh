#!/bin/zsh
# Convert every audio+EPUB pair in a folder.
#
# Usage: convert-batch.sh <folder> [--no-evict]
#
# Pairing convention (same as the Defiance of the Fall folder):
#   audio: "Volume N.mp3" / "Volume N.m4a"   (N = 1, 2, ...)
#   epub:  anything ending in _NN.epub       (NN zero-padded, e.g. *_01.epub)
# Unmatched files are reported and skipped.
set -u

FOLDER="${1:?usage: convert-batch.sh <folder> [--no-evict]}"
KIT_DIR="${0:A:h}"
EXTRA="${2:-}"
MIN_FREE_GB=8

[[ -d "$FOLDER" ]] || { echo "not a directory: $FOLDER"; exit 1; }

typeset -A audio_by_n epub_by_n
for f in "$FOLDER"/Volume\ *.(mp3|m4a)(N); do
  n="${${f:t:r}#Volume }"
  [[ "$n" == <-> ]] && audio_by_n[$n]="$f"
done
for f in "$FOLDER"/*_<->.epub(N); do
  n="${${f:t:r}##*_}"
  n=$((10#$n))
  epub_by_n[$n]="$f"
done

pairs=()
for n in ${(kon)audio_by_n}; do
  if [[ -n "${epub_by_n[$n]:-}" ]]; then
    pairs+=("$n")
  else
    echo "SKIP: Volume $n has audio but no matching *_$(printf '%02d' $n).epub"
  fi
done
for n in ${(kon)epub_by_n}; do
  [[ -z "${audio_by_n[$n]:-}" ]] && echo "note: EPUB $n has no matching audio file"
done
(( ${#pairs} )) || { echo "no convertible pairs found in $FOLDER"; exit 1; }
echo "converting ${#pairs} book(s): volumes ${pairs[*]}"

fail=0
for n in "${pairs[@]}"; do
  free_gb=$(df -g "$HOME" | awk 'NR==2 {print $4}')
  if (( free_gb < MIN_FREE_GB )); then
    echo "STOPPING: only ${free_gb}GB free (need ${MIN_FREE_GB}GB). Free space and re-run to resume."
    fail=1; break
  fi
  echo "##### VOLUME $n #####"
  if zsh "$KIT_DIR/convert.sh" "${audio_by_n[$n]}" "${epub_by_n[$n]}" "" $EXTRA; then
    echo "VOLUME $n COMPLETE"
  else
    echo "VOLUME $n FAILED — continuing with the next book"
    fail=1
  fi
done
echo "BATCH DONE (fail=$fail)"
exit $fail
