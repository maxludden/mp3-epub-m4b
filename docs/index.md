![mp3-epub-m4b Banner](img/banner.png)

# mp3+epub=m4b

Turns a single-file audiobook (`.mp3` / `.m4a`) plus its EPUB into a chaptered
`.m4b` with accurate chapter boundaries (detected from silence gaps in the audio,
anchored chapter-by-chapter so they can't drift), embedded cover art, and
optionally the opening/end credits split into their own chapters.

## Quick start

```shell
uv tool install .
mp3-epub-m4b convert "Some Book.mp3"   # uses "Some Book.epub" beside it
mp3-epub-m4b batch "/path/to/series folder"
```

See [Installation](installation.md) to get set up, then [Convert one book](convert.md) or [Convert a series](batch.md).

<div align="center">
    <br />
    <a href="https://github.com/maxludden" style="text-decoration:none; color:inherit;">
        <img
        src="img/MaxLogo.svg"
        alt="Max Ludden's Logo"
        width="20%"
        />
    </a>
    <a href="https://github.com/maxludden" style="text-decoration:none; color:inherit;">
        <p>Designed by Max Ludden</p>
    </a>
</div>
