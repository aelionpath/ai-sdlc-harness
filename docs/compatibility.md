# Compatibility

AI SDLC Harness requires Python 3.10 or newer.

Core logic is designed to be cross-platform through Python standard-library file handling. Local maintainer validation has been performed on Windows. macOS and Linux are designed for OS-neutral compatibility but have not yet been manually validated by the maintainer.

The CLI avoids shell-specific assumptions, platform-specific dependencies, and automatic execution of project commands.

Package-registry publication is not part of the current implemented release surface.
