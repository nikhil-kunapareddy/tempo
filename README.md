![Tempo](assets/image.png)

# Tempo

A local-first AI assistant for macOS — chat with a model and get real things done, starting with your calendar.

> 🚧 **Work in progress.** Tempo is an Apple Silicon desktop app under active development.

## About

Tempo is a **fork of [OpenWorker](https://github.com/andrewyng/openworker)**, deliberately pared down to one focused experience:

- **One model provider** — Together AI (bring your own key).
- **One integration** — Google Calendar (read your schedule, create events with your approval).
- **Local-first** — runs on your Mac; your data only leaves it through the services you connect.

The original OpenWorker codebase is preserved under [`open-worker/`](./open-worker) for reference while Tempo is built fresh on top of it.

## Status

Early days. The core is working as a local web app (Together chat + Google Calendar with approval-gated writes); the native macOS desktop shell and packaging are next.
