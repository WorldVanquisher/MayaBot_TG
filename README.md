# MayaBot – Self-Hosted Telegram Bot

## Overview
MayaBot is a personal, self-hosted Telegram bot designed as a unified interface for interacting with external APIs and self-managed infrastructure services.

The bot is deployed on a Linux-based NAS using Docker and is intended for incremental feature expansion rather than as a single-purpose chatbot.

---

## Features
- Modular command and handler structure for easy feature addition
- Integration with third-party APIs through isolated adapter modules
- Designed for long-running operation in a self-hosted environment

---

## Architecture
The bot follows a modular design:
- Core bot logic handles message routing and command dispatch
- Feature-specific logic and external service integrations are separated into independent modules
- Configuration and secrets are injected via environment variables

This structure allows new features to be added without modifying the core bot logic.

---

## Deployment
MayaBot is containerized using Docker and deployed on a Linux-based NAS.

The deployment process emphasizes:
- Reproducible environments
- Isolation between services
- Practical operability in real-world network conditions

---

## Notes
This project is primarily a personal infrastructure tool and a learning project focused on self-hosted service operation, debugging, and long-term maintainability.
