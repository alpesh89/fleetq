---
name: Handler proposal
about: Propose a new task kind before building it
labels: handler
---

**Kind name** (`KIND = "..."`):

**What one task does** (input payload, what it produces, where it stores it):

**Worker requirements** (`NEEDS` tags, OS, packages, external services or credentials):

**Rate limits and politeness** (what remote systems it talks to and how it paces them):

**Fan-out** (does it spawn other tasks? which kinds, how many?):

**Built-in or separate package?** (no paid or proprietary dependency → built-in is fine)
