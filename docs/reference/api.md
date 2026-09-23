<!-- generated:start cap:api-intro -->
# API Reference

Endpoints **declared on the architecture canvas** (`endpoints` widgets) - not extracted from source. Reconciliation against live routes is tooling-owned and will operate inside these same generated blocks.
<!-- generated:end cap:api-intro -->

<!-- generated:start comp:shortcut-server -->
## Shortcut Server (`shortcut-server`)

| Method | Path | Description |
|---|---|---|
| **GET** | `/screenshot/{obsId}` | Raw screenshot bytes for an observation, served with long-lived caching. |
| **GET** | `/health` | Liveness/status: species count loaded, OCR availability, collection presence. |
| **POST** | `/appraise` | POST a screenshot image; returns verdict JSON, or plain text with ?format=text. Records to Collection DB and pushes to ntfy when configured. |
| **DELETE** | `/pokemon/{entityId}` | Mark an entity as disposed (transferred/traded away). |
| **PATCH** | `/pokemon/{entityId}` | Update an observation's fields (JSON body); recomputes PvP rank and verdict context. Optional user_tag sets the entity's sticky manual tag override (one of the nine tags; null/"" clears it). A user_tag-only body skips re-evaluation. |
| **GET** | `/` | Web UI: appraised collection as a sortable table (requires --collection). Responsive: at ≤640px it hides secondary columns and adds a sort select, thumb-sized actions and a bottom-sheet editor. Shows the manual tag override over the computed tag. |
<!-- generated:end comp:shortcut-server -->
