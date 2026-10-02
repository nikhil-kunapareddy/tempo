![Tempo](assets/image.png)

# Tempo

A local-first AI assistant for macOS — chat with a model and get real things done, starting with your calendar.

> 🚧 **Work in progress.** Tempo is an Apple Silicon desktop app under active development.

## Download

**[Go to the downloads page](https://github.com/nikhil-kunapareddy/tempo/releases/latest)** and
click the file ending in `.dmg`.

Requires an **Apple Silicon Mac** (M1 or newer) running **macOS 12 or later**.

### Installing

1. Open the `.dmg` and drag Tempo into your **Applications** folder. Run it from there, not
   from the disk image: Tempo can only update itself where it's installed.
2. The first time, macOS stops it with "Apple could not verify Tempo is free of malware",
   because the app isn't signed with an Apple developer certificate yet. Click **Done**, go
   to **System Settings → Privacy & Security**, scroll to the message about Tempo, click
   **Open Anyway**, and confirm. (On macOS 14 and earlier, right-clicking Tempo and choosing
   **Open** also works.)

You only need to do this once.

### Setup

Tempo is bring-your-own-key — nothing is shared with us, and there's no account to create.

1. **Together AI key.** Open **Settings** in Tempo and paste a key from
   [api.together.ai](https://api.together.ai/settings/api-keys), then pick a model.
2. **Google Calendar.** Click **Connect Google Calendar**. Consent opens in your normal
   browser (Google blocks sign-in inside embedded app windows); approve access, then come
   back to Tempo.

> **During the beta**, Tempo's Google app is still in testing, so Google only lets
> approved accounts connect. If you see `Error 403: access_denied`, send us the Google
> account address you want to use and we'll add it to the tester list.

Calendar reads happen automatically; anything that **writes** to your calendar is shown to
you for approval before it runs.

### Updates

When Tempo starts, it asks GitHub whether a newer version is out. If one is, it downloads it
in the background, checks that it's signed by us, and offers to restart into it. It never
restarts on its own. Turn the check off in **Settings**.

<details>
<summary><b>Does anything get sent over the internet?</b></summary>

Only three things, all of them yours to control: your chats go to Together AI with your key,
calendar requests go to Google once you connect it, and the update check asks GitHub for the
latest release. There's no account, no analytics, and nothing goes to us.

</details>

## About

Tempo is a **fork of [OpenWorker](https://github.com/andrewyng/openworker)**, deliberately pared down to one focused experience:

- **One model provider** — Together AI (bring your own key).
- **One integration** — Google Calendar (read your schedule, create events with your approval).
- **Local-first** — runs on your Mac; your data only leaves it through the services you connect.

The original OpenWorker codebase is preserved under [`open-worker/`](./open-worker) for reference while Tempo is built fresh on top of it.

## Status

Early days, but the pieces are in place: Together chat, Google Calendar with approval-gated
writes, and a native macOS window around them.

Tempo is Python end to end — the desktop shell is a WKWebView driven from Python via pyobjc
(`desktop/shell.py`), so the server and the window share one process and there's no Rust or
Node in the build.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m desktop.main              # the desktop app
.venv/bin/python -m desktop.main --headless   # server only, open localhost:8000 yourself
```

Packaging (Apple Silicon, needs `pyinstaller`): `packaging/build_dmg.sh` freezes everything
into `Tempo.app`, wraps it in a `.dmg`, and writes the signed update archive the in-app
updater downloads. See the header of that script for signing and notarization. CI
(`.github/workflows/desktop.yml`) runs the same script on every push and pull request.

## Releasing

Changes go out as betas first; a release reaches installed copies only once it's promoted.

1. In a PR, bump `__version__` in `desktop/__init__.py` and move the `[Unreleased]` notes in
   `CHANGELOG.md` under the new version.
2. Once it's merged, run `python scripts/release.py` from `main`. It tags the next beta
   (`v0.2.0-beta.1`, …) and creates the GitHub release as a pre-release; `rc` and `stable`
   pick the other channels, and `--dry` just prints the tag.
3. CI builds the `.dmg`, the update archive, its signature and `latest.json`, and attaches
   them to the release.
4. For a stable release, promote it once those are attached. Installed copies then offer
   the update:

   ```sh
   gh release edit v0.2.0 --prerelease=false --latest
   ```

CI needs three repository secrets: `TEMPO_UPDATE_PRIVATE_KEY` (signs updates; the app only
installs updates signed with it), `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (the OAuth
client baked into the app).
