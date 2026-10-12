class ScrapeError(Exception):
    """Base class for anything that should fail a scrape run."""


class BlockedError(ScrapeError):
    """The source is refusing us (403/429/challenge page)."""


class ParseError(ScrapeError):
    """The page came back but does not look like what the parser expects."""
