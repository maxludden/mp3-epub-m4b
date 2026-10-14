"""mp3-epub-m4b: convert single-file audiobooks plus EPUBs into chaptered M4B files.

Library logging uses loguru and is disabled by default, as loguru recommends for
libraries. Applications opt in with ``logger.enable("mp3_epub_m4b")`` (the CLI does).
"""

from loguru import logger

logger.disable("mp3_epub_m4b")
