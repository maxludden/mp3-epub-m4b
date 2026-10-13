"""audiobook-kit: convert single-file audiobooks plus EPUBs into chaptered M4B files.

Library logging uses loguru and is disabled by default, as loguru recommends for
libraries. Applications opt in with ``logger.enable("audiobook_kit")`` (the CLI does).
"""

from loguru import logger

logger.disable("audiobook_kit")
