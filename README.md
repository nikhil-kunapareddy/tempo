![Tempo](assets/image.png)

# Tempo

A local-first AI assistant for macOS — chat with a model and get real things done, starting with your calendar.

> 🚧 **Work in progress.** Tempo is an Apple Silicon desktop app under active development.

## Download the beta

[**⬇ Download Tempo for macOS**](https://github.com/nikhil-kunapareddy/tempo/releases/latest/download/Tempo-AppleSilicon.dmg)

Requires an **Apple Silicon Mac** running **macOS 12 or later**. Open the `.dmg` and drag
Tempo to your Applications folder.

### First launch

The beta isn't code-signed yet, so macOS will refuse to open it on the first try
("Apple could not verify Tempo is free of malware"). To get past it:

1. Right-click Tempo in Applications and choose **Open**.
2. If macOS still blocks it, go to **System Settings → Privacy & Security**, scroll to the
   message about Tempo, and click **Open Anyway**.

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

## About

Tempo is a **fork of [OpenWorker](https://github.com/andrewyng/openworker)**, deliberately pared down to one focused experience:

- **One model provider** — Together AI (bring your own key).
- **One integration** — Google Calendar (read your schedule, create events with your approval).
- **Local-first** — runs on your Mac; your data only leaves it through the services you connect.

The original OpenWorker codebase is preserved under [`open-worker/`](./open-worker) for reference while Tempo is built fresh on top of it.

## Status

Early days. The core is working as a local web app (Together chat + Google Calendar with approval-gated writes); the native macOS desktop shell and packaging are next.
