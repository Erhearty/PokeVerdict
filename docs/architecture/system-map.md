<!-- generated:start file:system-map -->
# System Map

```mermaid
graph TD
    calibration-tool["Calibration Tool <br/> <small>(CUSTOM)</small>"]
    collection-db["Collection DB <br/> <small>(DATABASE)</small>"]
    game-data-builder["Game Data Builder <br/> <small>(CUSTOM)</small>"]
    game-data-db["Game Data DB <br/> <small>(DATABASE)</small>"]
    ios-shortcut["iOS Shortcut <br/> <small>(FRONTEND)</small>"]
    iv-solver["IV Solver <br/> <small>(CUSTOM)</small>"]
    ntfy-push["ntfy Push <br/> <small>(GATEWAY)</small>"]
    shortcut-server["Shortcut Server <br/> <small>(BACKEND)</small>"]
    calibration-tool -->|SQLite (read)| game-data-db
    game-data-builder -->|SQLite (write, offline build)| game-data-db
    ios-shortcut -->|HTTP POST (image upload)| shortcut-server
    iv-solver -->|SQLite (read)| game-data-db
    shortcut-server -->|SQLite (read/write)| collection-db
    shortcut-server -->|Python function call| iv-solver
    shortcut-server -->|HTTPS POST| ntfy-push
```

## Components

- [Calibration Tool](overview.md) (`calibration-tool`, custom)
- [Collection DB](overview.md) (`collection-db`, database)
- [Game Data Builder](overview.md) (`game-data-builder`, custom)
- [Game Data DB](overview.md) (`game-data-db`, database)
- [iOS Shortcut](overview.md) (`ios-shortcut`, frontend)
- [IV Solver](overview.md) (`iv-solver`, custom)
- [ntfy Push](overview.md) (`ntfy-push`, gateway)
- [Shortcut Server](overview.md) (`shortcut-server`, backend)

## Interactions

- [calibration-tool → game-data-db](interactions/calibration-tool--game-data-db.md) via `SQLite (read)`
- [game-data-builder → game-data-db](interactions/game-data-builder--game-data-db.md) via `SQLite (write, offline build)`
- [ios-shortcut → shortcut-server](interactions/ios-shortcut--shortcut-server.md) via `HTTP POST (image upload)`
- [iv-solver → game-data-db](interactions/iv-solver--game-data-db.md) via `SQLite (read)`
- [shortcut-server → collection-db](interactions/shortcut-server--collection-db.md) via `SQLite (read/write)`
- [shortcut-server → iv-solver](interactions/shortcut-server--iv-solver.md) via `Python function call`
- [shortcut-server → ntfy-push](interactions/shortcut-server--ntfy-push.md) via `HTTPS POST`
<!-- generated:end file:system-map -->
