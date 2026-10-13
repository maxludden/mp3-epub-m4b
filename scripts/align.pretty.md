# stalign

A CLI to automatically align audiobooks and EPUB files, producing EPUBs with Media Overlays.

`stalign` can be called with no subcommand to run a full alignment pipeline on a set of input files, or run with one subcommend at a time (`process`, `transcribe`/`emit`, `markup`, `align`) to run individual alignment steps.

Run `stalign help [subcommand]` to see arguments and examples for individual subcommands. The arguments listed below are for the top-level command, which runs the entire alignment pipeline.

## Usage

### `align process`

```
align process
  [--parallel INTEGER]
  [--codec TYPE] [--bitrate BITRATE] [--max-length INTEGER]
  [--no-progress] [--log-level TYPE] [--time]
  INPUT OUTPUT
```

### `align transcribe`

```
align transcribe
  [--parallel INTEGER] [--language LOCALE]
  ( --engine/-e whisper.cpp
      [--model/-m MODEL] [--threads INTEGER] [--processors INTEGER]
      [--cpu-override CPU_ENGINE]
  | --engine/-e whisper-server
      --whisper-server-url URL [--whisper-server-api-key STRING]
  | --engine/-e openai-cloud
      [--openai-model STRING] [--openai-api-key STRING]
      [--openai-organization STRING] [--openai-base-url URL]
  | --engine/-e google-cloud
      --google-cloud-api-key STRING
  | --engine/-e microsoft-azure
      --azure-service-region STRING --azure-subscription-key STRING
  | --engine/-e amazon-transcribe
      --amazon-transcribe-region STRING
      --amazon-transcribe-access-key-id STRING
      --amazon-transcribe-secret-access-key STRING
  | --engine/-e deepgram
      --deepgram-api-key STRING [--deepgram-model STRING] )
  [--no-progress] [--log-level TYPE] [--time]
  INPUT OUTPUT
```

### `align emit`

```
align emit
  [--engine ENGINE] [--device/-d DEVICE] [--model MODEL] [--dtype PRECISION]
  [--batch-size INTEGER] [--concurrency INTEGER]
  [--server-url URL] [--server-api-key STRING]
  [--language LOCALE]
  [--no-progress] [--log-level TYPE] [--time]
  INPUT OUTPUT
```

### `align upgrade-epub`

```
align upgrade-epub [--remove-ncx] EPUB2 OUTPUT
```

### `align markup`

```
align markup
  [--granularity/-g TYPE] [--autoupgrade/-u] [--remove-ncx]
  [--language LOCALE]
  [--no-progress] [--log-level TYPE] [--time]
  INPUT_PATH OUTPUT_PATH
```

### `align align`

```
align align
  --output PATH
  ( --ctc --emissions PATH | --transcriptions PATH )
  --audiobook PATH --epub PATH
  [--text-ref TYPE] [--reports PATH] [--out-format TYPE]
  [--gap-seconds NUMBER] [--audio-chapter-seconds NUMBER] [--drop-unread]
  [--autoupgrade/-u] [--remove-ncx]
  [--no-progress] [--log-level TYPE] [--time]
  [--language LOCALE] [--granularity/-g TYPE]
```

### `align` (full pipeline)

```
align
  [--processed-audio PATH] [--markedup PATH]
  [--parallel-transcodes INTEGER] [--parallel-transcribes INTEGER]
  --output PATH
  [--codec TYPE] [--bitrate BITRATE] [--max-length INTEGER]
  ( --ctc
      [--emissions PATH] [--engine ENGINE] [--device/-d DEVICE]
      [--model MODEL] [--dtype PRECISION] [--batch-size INTEGER]
      [--concurrency INTEGER] [--server-url URL] [--server-api-key STRING]
  | --engine/-e whisper.cpp
      [--model/-m MODEL] [--threads INTEGER] [--processors INTEGER]
      [--cpu-override CPU_ENGINE] [--transcriptions PATH]
  | --engine/-e whisper-server
      --whisper-server-url URL [--whisper-server-api-key STRING]
      [--transcriptions PATH]
  | --engine/-e openai-cloud
      [--openai-model STRING] [--openai-api-key STRING]
      [--openai-organization STRING] [--openai-base-url URL]
      [--transcriptions PATH]
  | --engine/-e google-cloud
      --google-cloud-api-key STRING [--transcriptions PATH]
  | --engine/-e microsoft-azure
      --azure-service-region STRING --azure-subscription-key STRING
      [--transcriptions PATH]
  | --engine/-e amazon-transcribe
      --amazon-transcribe-region STRING
      --amazon-transcribe-access-key-id STRING
      --amazon-transcribe-secret-access-key STRING
      [--transcriptions PATH]
  | --engine/-e deepgram
      --deepgram-api-key STRING [--deepgram-model STRING]
      [--transcriptions PATH] )
  [--autoupgrade/-u] [--remove-ncx]
  [--granularity/-g TYPE] [--language LOCALE]
  --audiobook PATH --epub PATH
  [--text-ref TYPE] [--reports PATH] [--out-format TYPE]
  [--gap-seconds NUMBER] [--audio-chapter-seconds NUMBER] [--drop-unread]
  [--no-progress] [--log-level TYPE] [--time]
```

### `align snapshot`

```
align snapshot
  --epub PATH
  ( --transcriptions PATH | --ctc --emissions PATH )
  [--no-progress] [--log-level TYPE] [--time]
  OUTPUT_PATH
```

### Other

```
align help [COMMAND...]
align completion [SHELL] [ARG...]
align --help
align --version
```

## Subcommands

| Command | Description |
| --- | --- |
| `process` | Process audiobook files for transcription. |
| `transcribe` | Transcribe a directory of audiobook files. |
| `emit` | Analyze a directory of audiobook files for CTC alignment. |
| `upgrade-epub` | Upgrade an EPUB 2 file to EPUB 3. |
| `markup` | Mark up an EPUB file at the provided granularity level. |
| `align` | Run forced alignment to determine where each sentence\|word is spoken in the audiobook and produce a new EPUB package with Media Overlays and embedded audio. |
| `snapshot` | Print a human-readable snapshot of the EPUB's alignment to a text file. |
| `help` | Show help information. |
| `completion` | Generate shell completion script or provide completions. |

## Options

### General

| Option | Description | Default | Choices |
| --- | --- | --- | --- |
| `--processed-audio PATH` | | | |
| `--markedup PATH` | | | |
| `--parallel-transcodes INTEGER` | | `1` | |
| `--parallel-transcribes INTEGER` | | `1` | |
| `--output PATH` | | | |
| `--ctc` | Use CTC emissions instead of transcriptions for alignment | | |
| `--emissions PATH` | | | |
| `--engine ENGINE` | Where to generate emissions. Unset runs on this machine, or on the server when `--server-url` is given. | | `ctc-local`, `ctc-server` |
| `--device`, `-d DEVICE` | The device to run inference on | `auto` | `auto`, `cpu`, `webgpu`, `dml`, `cuda`, `mlx` |
| `--model MODEL` | The CTC model family to generate emissions with. `mms` handles any language and picks its precision from the device, the others need a `--language` they have a model for. | `mms` | `mms`, `quartznet15x5`, `citrinet256`, `citrinet512`, `citrinet1024` |
| `--dtype PRECISION` | The precision to run the model in on a GPU. CPU always runs fp32. Unset uses fp16 for `mms` and fp32 for the other families. | | `fp32`, `fp16` |
| `--batch-size INTEGER` | How many chunks of audio go through the model per call. Higher is faster on a GPU and needs more memory. Unset picks 2 on a GPU and 1 on a CPU. | | |
| `--concurrency INTEGER` | How many ffmpeg processes decode audio ahead of the model. Unset picks 4, or fewer on small machines. | | |
| `--server-url URL` | The URL of a ghost-story server started with `ghost-story serve` | | |
| `--server-api-key STRING` | The API key of the ghost-story server | | |
| `--transcriptions PATH` | | | |
| `--autoupgrade`, `-u` | Automatically upgrade any EPUB 2 files to EPUB 3 | | |
| `--remove-ncx` | Whether to remove the NCX and guide element after upgrading an EPUB from 2 to 3 | | |
| `--granularity`, `-g TYPE` | | `sentence` | `word`, `sentence` |
| `--language LOCALE` | BCP 47 language tag representing the primary language of the audio (e.g. `en-US`) | | |
| `--no-progress` | Disable the progress bar | | |
| `--log-level TYPE` | Log level. If enabled, will disable progress bar. | `silent` | `silent`, `debug`, `info`, `warn`, `error` |
| `--time` | Whether to print timing statistics | | |
| `--help` | Show help information. | | |
| `--version` | Show version information. | | |

### Audio processing

| Option | Description | Default | Choices |
| --- | --- | --- | --- |
| `--codec TYPE` | The audio codec to transcode to. If unspecified, will copy audio data without transcoding. | `copy` | `libopus`, `libmp3lame`, `aac`, `copy` |
| `--bitrate BITRATE` | The audio bitrate to transcode to | | |
| `--max-length INTEGER` | The maximum allowed length of a processed audio track, in minutes | `120` | |

### Transcription engines

#### whisper.cpp

| Option | Description | Default | Choices |
| --- | --- | --- | --- |
| `--engine`, `-e` | | | `whisper.cpp` |
| `--model`, `-m MODEL` | The whisper model to use | `tiny.en` | `tiny`, `tiny.en`, `tiny-q5_1`, `tiny.en-q5_1`, `tiny-q8_0`, `base`, `base.en`, `base-q5_1`, ... |
| `--threads INTEGER` | | `4` | |
| `--processors INTEGER` | The number of processors to use (values greater than 1 may affect timing accuracy) | `1` | |
| `--cpu-override CPU_ENGINE` | When provided, will use this whisper variant even if another is available | | `blas`, `cpu` |

#### whisper-server

| Option | Choices |
| --- | --- |
| `--engine`, `-e` | `whisper-server` |
| `--whisper-server-url URL` | |
| `--whisper-server-api-key STRING` | |

#### openai-cloud

| Option | Choices |
| --- | --- |
| `--engine`, `-e` | `openai-cloud` |
| `--openai-model STRING` | |
| `--openai-api-key STRING` | |
| `--openai-organization STRING` | |
| `--openai-base-url URL` | |

#### google-cloud

| Option | Choices |
| --- | --- |
| `--engine`, `-e` | `google-cloud` |
| `--google-cloud-api-key STRING` | |

#### microsoft-azure

| Option | Choices |
| --- | --- |
| `--engine`, `-e` | `microsoft-azure` |
| `--azure-service-region STRING` | |
| `--azure-subscription-key STRING` | |

#### amazon-transcribe

| Option | Choices |
| --- | --- |
| `--engine`, `-e` | `amazon-transcribe` |
| `--amazon-transcribe-region STRING` | |
| `--amazon-transcribe-access-key-id STRING` | |
| `--amazon-transcribe-secret-access-key STRING` | |

#### deepgram

| Option | Default | Choices |
| --- | --- | --- |
| `--engine`, `-e` | | `deepgram` |
| `--deepgram-api-key STRING` | | |
| `--deepgram-model STRING` | `nova-3` | |

### Alignment

| Option | Description | Default | Choices |
| --- | --- | --- | --- |
| `--audiobook PATH` | | | |
| `--epub PATH` | | | |
| `--text-ref TYPE` | Whether to use text fragments or element id fragments to identify text ranges in generated media overlays. | `id-fragment` | `id-fragment`, `text-fragment` |
| `--reports PATH` | | | |
| `--out-format TYPE` | Whether to output a full EPUB 3 package with embedded media overlays and audio, or a Readium Guided Navigation Package with just a manifest and guided navigation documents. | `epub` | `epub`, `gnp` |
| `--gap-seconds NUMBER` | Stretches of audio longer than this with no matching text become audio-only media overlay entries instead of being folded into the previous sentence. | | |
| `--audio-chapter-seconds NUMBER` | Audio-only stretches between chapters at least this long become their own chapters in the output. | | |
| `--drop-unread` | Give sentences the narrator did not read no audio instead of an interpolated clip. | | |

## Examples

Run the CTC aligner on an input audiobook and ebook (the `--emissions` flag is just used for caching emissions output in the case of a re-run):

```sh
stalign --ctc \
  --audiobook ./peter-and-wendy/audiobook \
  --epub "./peter-and-wendy/text/Peter and Wendy.epub" \
  --emissions ./cache/peter-and-wendy/emissions \
  --output "./peter-and-wendy/aligned/Peter and Wendy (readaloud).epub"
```

Run just the emissions step on an Apple computer with MLX and the quartznet model:

```sh
staling emit --model quartznet15x5 --language en --device mlx --dtype fp16 \
  ./peter-and-wendy/audiobook \
  ./cache/peter-and-wendy/emissions
```

Run just the markup step with multi-level (sentence and word) granularity:

```sh
stalign markup --granularity word --language en \
  "./peter-and-wendy/text/Peter and Wendy.epub" \
  "./cache/peter-and-wendy/markedup/Peter and Wendy (marked up).epub"
```

Run just the align step on the output of the emissions and markup examples:

```sh
stalign align --ctc \
  --audiobook ./peter-and-wendy/audiobook \
  --epub "./cache/peter-and-wendy/markedup/Peter and Wendy (marked up).epub" \
  --emissions ./cache/peter-and-wendy/emissions \
  --output "./peter-and-wendy/aligned/Peter and Wendy (readaloud).epub"
```
