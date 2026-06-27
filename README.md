# MayaBot – Self-Hosted Telegram Bot

English | [中文](README.zh-CN.md)

---

## Overview
MayaBot is a **self-hosted Telegram bot** designed for personal and small-group usage, driven primarily by personal interest and experimentation.

It serves as a unified interface for interacting with external APIs and self-managed infrastructure services.  
The bot is intended to run on a Linux-based NAS and is deployed via Docker.

Rather than being a single-purpose chatbot, MayaBot is designed as a **long-running, incrementally extensible personal infrastructure project**, and also acts as a testbed for validating personal project design ideas.

The primary interaction language of MayaBot is **Chinese**. Command design, prompt construction, and generated content are all optimized for Chinese linguistic context and Chinese internet culture.

---

## Development Notes & Blog (Ongoing)

- [Fixing NAS networking issues: when `ping` works but `curl` fails](https://vanquisher.world/2026/01/09/When-ping-works-but-curl-fails/)

This section documents real-world operational issues encountered during development and self-hosting.

---

## Features
- Modular command and handler architecture for rapid and continuous feature expansion  
- Integration with third-party APIs through isolated adapter modules  
- Chinese-first output and content generation logic  
- Designed for stable, long-running self-hosted deployment  

### Representative Commands
- **/dongzhuo**  
  Integrates Volcengine’s image generation API.  
  User-provided images are uploaded to Volcengine TOS (object storage) to construct an image processing pipeline.  
  Based on user input, the bot generates a predefined meme template (commonly referred to internally as the *“Dong Zhuo / Lü Bu”* meme).

  This command primarily serves as an experiment in **multi-stage image processing pipelines and meme template abstraction**, rather than as a standalone meme generator.

- **/fortune**  
  Generates a daily fortune for the user.  
  Supports limited re-draws when the generated value falls below a predefined threshold.

- **/music**  
  Searches and downloads audio from YouTube via an embedded `yt-dlp` + `ffmpeg` pipeline.  
  Accepts either keywords or a direct link; search results are listed as inline buttons, and the selected track is delivered as an audio file with embedded metadata and cover art.  
  Each download and search is persisted per user as a signal for the recommendation engine.

- **/randommusic**  
  Personalized song recommendation.  
  Builds seeds from the user's download history, search keywords, and learned artist preferences, then ranks candidates with weighted random sampling (so repeated calls vary).  
  Recommendations are delivered as audio with 👍 / 👎 feedback buttons: 👍 boosts that artist's weight, 👎 deletes the message and lowers it.  
  Manual calls return **1 track**; `/randommusic on|off` toggles a daily push (**3 tracks** at 08:00 Beijing time).

---

## Architecture
MayaBot follows a modular and decoupled architecture:

- **Core Layer**
  - Telegram message polling and dispatch  
  - Command parsing and routing  
  - Unified error handling and logging  

- **Feature Modules**
  - Independent command handlers  
  - AI image generation module (Volcengine / Doubao)  
  - Music fetching & recommendation module (`yt-dlp` + `ffmpeg`, per-user SQLite persistence)  
  - Reserved interfaces for future feature expansion  

- **Configuration Management**
  - All configuration and secrets are injected via environment variables  
  - No credentials are stored in the codebase  
  - Per-user data (download history, preferences, subscriptions) persisted in a mounted SQLite volume  

This design allows new features to be added without modifying the core bot logic.

---

## Deployment
MayaBot is fully containerized using Docker and deployed on a Linux-based NAS.

Deployment priorities include:
- Reproducible runtime environments  
- Isolation between services  
- Tolerance for unstable or constrained home network conditions  

The bot is designed to operate continuously and degrade gracefully when encountering network or external API failures.

---

## Language & Usage Notes
- MayaBot is primarily designed for **Chinese-speaking users**
- Prompts, responses, and meme logic assume Chinese linguistic and cultural context
- Practical usability is prioritized over full multilingual coverage

---

## Project Scope
This project is explicitly scoped as:
- A **personal infrastructure tool**
- A learning and experimentation platform focused on:
  - Self-hosted service operation
  - Docker-based deployment
  - Third-party API integration
  - Long-term maintenance and debugging

It is **not** intended to be a public SaaS product or a general-purpose chatbot.

---

## Future Work
- Add SoundCloud as a download/recommendation source (pending a stable extraction handler)  
- Introduce multilingual support to allow usage by non-Chinese speakers  
- Expose limited NAS storage as a small-scale personal file cloud for trusted users  
- Continuously refine user experience and add new features driven by personal interest
