# Security notes for maintainers and reviewers

Game Installer is intended to download, extract, and launch games selected by the user. Its Defender integration is intended to reduce interruptions when downloaded game files or third-party mod DLLs are quarantined during installation. It is not intended to capture keystrokes or conceal malicious software. This statement describes the project's stated intent, not a guarantee that every downloadable game or DLL is safe.

## Windows Defender exclusions

In `main.py`, the `defender_exclusion_default` preference controls whether the installer tries to add the selected game install directory and the current user's entire temporary directory to Microsoft Defender's path exclusions. The preference currently defaults to enabled when unset and can be changed in Settings. Adding or removing exclusions calls Windows PowerShell's `Add-MpPreference` and `Remove-MpPreference`; the helper may request elevation if needed.

The installation path exclusion is not automatically removed when installation finishes. The installer attempts to remove a temporary-directory exclusion that it added itself after extraction or on an early-exit cleanup path; a pre-existing exclusion is left in place. Cleanup is not guaranteed after a crash or forced termination. Exclusions affect Defender scanning of everything under those paths, not just the intended game, and an exclusion on the entire temp directory can expose unrelated files. Review existing exclusions in Windows Security and remove any that are no longer wanted.

Treat detections as unverified until investigated: a custom DLL can be a false positive, but it can also be malicious. Prefer investigating the detection and obtaining trusted files over adding broad exclusions. Do not treat this note as evidence that a particular file is safe or that an antivirus alert should be ignored. Downloaded games, patches, bundled tools, and their source hosts need separate review.

## Scope of review

An investigation of repository source should distinguish this installer from executables and DLLs it downloads or launches. A text search cannot prove absence of malware, especially in binary dependencies, downloaded packages, generated artifacts, or remote services. Review the actual release artifacts and dependencies independently; never use the stated intent above as a substitute for verification.
