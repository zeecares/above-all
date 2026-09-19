"""Provider memory contract.

Above-all is the single durable owner of curated memory. A provider's native
context file (for example CLAUDE.md for Claude Code) is only a *delivery
surface* for approved memory, plus whatever private scratch the user or the
provider keeps there. Provider-native memory is read-only input: it may be
imported as review-only candidates and it never becomes a second automatic
writer of durable memory. Pi delivery stays disabled until a real fixture
verifies its native format (ZEE-57); do not register an unverified provider.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """How one agent CLI receives managed context and exposes native memory."""

    name: str
    context_filename: str  # project-root file the provider reads natively
    verified_against: str  # the exact behavior this registration was verified against


_PROVIDERS = {
    "claude-code": ProviderSpec(
        name="claude-code",
        context_filename="CLAUDE.md",
        verified_against=(
            "stock public Claude Code: reads CLAUDE.md at the project root as "
            "project memory; internal builds are reported very similar (ZEE-57)"
        ),
    ),
}


def get_provider(name: str) -> ProviderSpec:
    """Return a verified provider spec, failing loudly for anything else."""
    spec = _PROVIDERS.get(name)
    if spec is not None:
        return spec
    available = ", ".join(sorted(_PROVIDERS))
    raise ValueError(
        f"unverified provider {name!r} (available: {available}); pi and internal "
        "builds stay disabled until one real fixture confirms their native "
        "format - see ZEE-57"
    )
