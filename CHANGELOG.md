# Changelog

All notable changes to Tempo are recorded here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- The macOS app is built by CI. Every push to `main` and every pull request runs the tests on Apple Silicon and produces a `.dmg` you can download from the run's summary.
- Releases on GitHub. Each release has the `.dmg` attached, and the [latest one](https://github.com/nikhil-kunapareddy/tempo/releases/latest) is always one click from the README. New versions go out as betas first and reach everyone only once promoted.
- Updates. When Tempo starts, it checks GitHub for a newer release, downloads it in the background, checks its signature, and offers to restart into it. It never restarts on its own. **Settings** has a switch to turn the check off.

### Changed
- The app is ad-hoc code-signed instead of unsigned. The first launch on each Mac still needs right-click → **Open**, because the app isn't signed with an Apple Developer ID yet.
- Downloads are named for their version, such as `Tempo_0.2.0_arm64.dmg`.

Copies installed before the first release with the updater have no way to update themselves, so moving to it is a one-time manual install. From then on, updates arrive on their own.
