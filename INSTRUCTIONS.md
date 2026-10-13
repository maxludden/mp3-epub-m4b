# Portable Audiobook Conversion Kit

## Context

Max has two spare computers at home and wants the perfected audiobook→chaptered-M4B
process packaged so it can run on any of his machines, not just this one. Today's
Defiance of the Fall run surfaced every failure mode (iCloud eviction, disk pressure,
opening-credits chapter shift, resumable-pipeline looping) and the fixes now live in
one-off scratchpad scripts tied to this session. The goal: a self-contained kit,
synced via iCloud, that any Mac can run with one setup command + one convert command.
The current 15-volume re-conversion keeps running here untouched; the kit is for
future series (Primal Hunter, HWFWM, etc.).

## Deliverable

`.../com~apple~CloudDocs/Books/audiobook-kit/` (in iCloud so the spare Macs receive
it automatically):

```shell
audiobook-kit/
  README.md          # one-page: setup once, then ./convert.sh
  setup.sh           # installs deps on a new machine
  convert.sh         # convert one book: convert.sh <audio> <epub> [out-dir]
  convert-batch.sh   # convert a folder: pairs Volume N.* with *_NN.epub, loops
  pipeline/          # copied scripts/ from the audiobook-mp3-to-m4b skill
```

## Implementation steps

1. **Copy the pipeline** — duplicate the `scripts/` directory of the
   `audiobook-mp3-to-m4b` skill (base:
   `/Users/maxludden/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/eff2f15b-ccde-42fc-b82b-ab5e19207c69/a1441aa9-9ec3-453b-a5e4-756ca74e8623/skills/audiobook-mp3-to-m4b/scripts/`)
   into `audiobook-kit/pipeline/`. First enumerate that directory to bring
   run.py's helper modules (align_chapters.py, epub_extract.py, …) along.

2. **setup.sh** — idempotent machine prep:
   - Homebrew present? If not, print install instructions and exit (don't install
     Homebrew unattended).
   - `brew install ffmpeg` if `ffmpeg`/`ffprobe` missing.
   - Create venv at `~/.audiobook-kit-venv` (local disk, NOT iCloud — venvs don't
     survive syncing) and `pip install beautifulsoup4 lxml`.

3. **convert.sh** — single book, all of today's lessons baked in:
   - `wait_local()` iCloud materialization guard (stat blocks vs size + `brctl
     download`) before starting; optional `--evict` to `brctl evict` the source after
     success (default on when the source lives under `Mobile Documents`).
   - Work dir on local disk (`~/.audiobook-kit-work`), never iCloud.
   - Loop `pipeline/run.py` with `--detect-intro --intro-max-len 120 --detect-outro
     --outro-max-tail 180 --time-budget 300` until `DONE:`; abort the book on
     `Traceback`/`Error`; cap iterations.
   - Move the finished m4b to the output dir (default: `<audio's folder>/m4b/`),
     clean the work state, print an `ffprobe -show_chapters` verification summary
     (chapter count, duration vs source, cover stream present).

4. **convert-batch.sh** — folder mode: find `Volume N.<ext>` audio + `*_NN.epub`
   pairs (same numbering convention as the DotF folder), report unmatched files,
   then run convert.sh per pair sequentially with a df space check between books.

5. **README.md** — setup once (`./setup.sh`), then either
   `./convert.sh "Volume 1.mp3" "book_01.epub"` or `./convert-batch.sh <folder>`;
   note the EPUB requirement, expected runtime (~35 min/book), and that re-running
   resumes rather than restarts.

6. **Wire into the skill** — add a short "Portable kit" note to
   `~/.claude/skills/audiobook-playlist-fetch/SKILL.md` Stage 4 pointing at the kit
   path, so future sessions use/maintain it instead of rebuilding scratchpad scripts.

## Reuse

- Driver logic: generalize `convert_all_v2.sh` (this session's scratchpad) — it
  already contains wait_local, the run.py loop, state cleanup, and eviction.
- Extractor/downloader for stages 1–3 stay in the `audiobook-playlist-fetch` skill;
  the kit is stage 4 only (conversion), so it works standalone on machines without
  Claude.

## Verification

1. `zsh -n` all three shell scripts (syntax).
2. Run `./setup.sh` on this machine — should detect existing ffmpeg, build the venv.
3. Smoke-test convert.sh against `Volume 1.mp3` + `defiance_of_the_fall_01.epub`
   with a tiny `--time-budget`, confirming it reaches the extract stage from kit
   paths, then Ctrl-out (full conversion already covered by tonight's run).
4. Confirm the kit folder appears in iCloud (`brctl status` optional) and README
   renders sensibly.
