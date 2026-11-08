# Changelog

All notable changes to AX are documented in this file. The detailed implementation-decision record remains in the [documentation changelog](docs/CHANGELOG.md).

This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

### Limitations

- Project onboarding and HQ identity delivery require deployment-specific validation; production second-factor notification is not connected.
- Several CLI operations remain unimplemented; see the current CLI guide.

### Added

- Core services and interfaces for orchestration, intake, approvals, resource management, delivery, notifications, and release subloops.
- Compose overlays, runtime graph validation, plugin and external-service registries, and their supporting schemas.
- Host deployment utilities, a command-line interface, configuration templates, and a public operator manual.

### Changed

- Extended subloop node discovery with declarative node directories and tightened validation of runtime and external delivery inputs.
- Generalized profile and environment documentation for public use.
- Prepared the tracked project tree for publication with licensing, contribution, conduct, security, and scope documents.

### Fixed

- Validated approval-denial factors and unavailable external targets before use.
- Aligned seat enrollment, mitigation, profile-secret, and offline-exam behavior with their schemas and documented contracts.
- Corrected public documentation examples and workflow guidance, including current CLI limits, model registration, Compose networks, and project templates.
