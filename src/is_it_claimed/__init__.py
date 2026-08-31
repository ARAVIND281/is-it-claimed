"""Check whether a GitHub issue is already being worked on, before you start."""

from is_it_claimed.core import GitHubError, Signal, Verdict, check, parse_target

__version__ = "0.1.0"
__all__ = ["check", "parse_target", "Verdict", "Signal", "GitHubError", "__version__"]
