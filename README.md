# MayaBot – Self-Hosted Telegram Bot

## Overview
MayaBot is a personal, self-hosted Telegram bot designed as a unified interface for interacting with external AI APIs and self-managed infrastructure services.

The bot is deployed on a Linux-based NAS using Docker and is intended to evolve incrementally as a long-running personal infrastructure project rather than a single-purpose chatbot.

The primary interaction language of MayaBot is **Chinese**, with most responses, prompts, and generated content optimized for Chinese users and internet culture.

---

## Features
- Modular command and handler architecture for incremental feature expansion
- Integration with external AI services via isolated adapter modules
- Chinese-first prompt design and output formatting
- Long-running, self-hosted deployment optimized for reliability

### Representative Capabilities
- **AI Image Generation (Volcengine / Doubao API)**  
  MayaBot integrates with Volcengine’s image generation APIs to produce internet-style meme images, including customized meme formats such as **“吕布 / 董卓”梗图**, generated dynamically based on user prompts.

- **API-Oriented Design**  
  External services (e.g., AI models, image generation, data services) are accessed through well-defined adapters, allowing services to be replaced or upgraded without affecting core bot logic.

---

## Architecture
MayaBot follows a modular and decoupled design:

- **Core Layer**
  - Telegram message polling
  - Command parsing and routing
  - Unified error handling and logging

- **Feature Modules**
  - Independent command handlers
  - AI image generation adapters (Volcengine / Doubao)
  - Future extensible modules for media, utilities, and personal services

- **Configuration**
  - All configuration and secrets are injected via environment variables
  - No credentials are stored in the codebase

This architecture allows new features to be added with minimal changes to existing code.

---

## Deployment
MayaBot is fully containerized using Docker and deployed on a Linux-based NAS.

Deployment principles include:
- Reproducible environments
- Service isolation
- Stability under real-world home network conditions

The bot is designed to run continuously and tolerate intermittent network or API failures.

---

## Language & Usage Notes
- MayaBot is primarily designed for **Chinese-language interaction**
- Prompts, responses, and meme generation logic assume Chinese linguistic context
- The project prioritizes practical usability over multilingual completeness

---

## Project Scope
This project is intentionally scoped as:
- A **personal infrastructure tool**
- A **learning project** focused on:
  - Self-hosted service operation
  - Docker-based deployment
  - API integration and debugging
  - Long-term maintainability

It is not intended to be a public SaaS product or a general-purpose chatbot.

---

## Future Work (Planned)
- Additional AI-powered content generation modules
- More meme templates and prompt abstractions
- Internal service integrations with other self-hosted tools
- Improved observability and health monitoring

