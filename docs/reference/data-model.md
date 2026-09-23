<!-- generated:start cap:data-model-intro -->
# Data Model

Projected from `schema` widgets on the architecture canvas.
<!-- generated:end cap:data-model-intro -->

<!-- generated:start comp:collection-db -->
## Collection DB (`collection-db`)

### entity

| Field | Type | Flags | Notes |
|---|---|---|---|
| `id` | INTEGER | Primary Key | - |
| `identity_key` | TEXT | - | - |
| `nickname` | TEXT | - | - |
| `user_tag` | TEXT | - | - |
| `is_disposed` | INTEGER | NOT NULL DEFAULT 0 | - |
| `notes` | TEXT | - | - |
| `first_seen_at` | TEXT | NOT NULL | - |
| `last_seen_at` | TEXT | NOT NULL | - |

### observation

| Field | Type | Flags | Notes |
|---|---|---|---|
| `id` | INTEGER | Primary Key | - |
| `captured_at` | TEXT | NOT NULL | - |
| `species_template_id` | TEXT | NOT NULL | - |
| `display_name` | TEXT | NOT NULL | - |
| `cp` | INTEGER | NOT NULL | - |
| `hp` | INTEGER | NOT NULL | Max HP, never current. |
| `stardust_cost` | INTEGER | - | - |
| `attack_iv` | INTEGER | - | - |
| `defense_iv` | INTEGER | - | - |
| `stamina_iv` | INTEGER | - | - |
| `iv_total` | INTEGER | - | - |
| `iv_certain` | INTEGER | NOT NULL DEFAULT 0 | - |
| `is_shiny` | INTEGER | NOT NULL DEFAULT 0 | - |
| `is_shadow` | INTEGER | NOT NULL DEFAULT 0 | - |
| `is_purified` | INTEGER | NOT NULL DEFAULT 0 | - |
| `is_lucky` | INTEGER | NOT NULL DEFAULT 0 | - |
| `is_costume` | INTEGER | NOT NULL DEFAULT 0 | - |
| `is_dynamax` | INTEGER | NOT NULL DEFAULT 0 | - |
| `size_class` | TEXT | - | - |
| `verdict` | TEXT | - | - |
| `verdict_tag` | TEXT | - | - |
| `verdict_reason` | TEXT | - | - |
| `gamedata_version` | TEXT | - | - |
| `entity_id` | INTEGER | FK -> entity(id) ON DELETE SET NULL | - |

### capture_failure / screenshot

| Field | Type | Flags | Notes |
|---|---|---|---|
| `capture_failure.id` | INTEGER | Primary Key | - |
| `capture_failure.captured_at` | TEXT | NOT NULL | - |
| `capture_failure.raw_text` | TEXT | - | - |
| `capture_failure.reason` | TEXT | NOT NULL | - |
| `capture_failure.screenshot` | BLOB | - | - |
| `capture_failure.mime_type` | TEXT | - | - |
| `screenshot.observation_id` | INTEGER | Primary Key, FK -> observation(id) ON DELETE CASCADE | - |
| `screenshot.data` | BLOB | NOT NULL | - |
| `screenshot.mime_type` | TEXT | NOT NULL DEFAULT 'image/jpeg' | - |
<!-- generated:end comp:collection-db -->

<!-- generated:start comp:game-data-db -->
## Game Data DB (`game-data-db`)

### species / cpm

| Field | Type | Flags | Notes |
|---|---|---|---|
| `species.template_id` | TEXT | Primary Key | - |
| `species.dex` | INTEGER | NOT NULL | - |
| `species.pokemon_id` | TEXT | NOT NULL | - |
| `species.form` | TEXT | - | - |
| `species.display_name` | TEXT | NOT NULL | - |
| `species.base_attack` | INTEGER | NOT NULL | - |
| `species.base_defense` | INTEGER | NOT NULL | - |
| `species.base_stamina` | INTEGER | NOT NULL | - |
| `species.type1` | TEXT | - | - |
| `species.type2` | TEXT | - | - |
| `species.family_id` | TEXT | - | - |
| `species.candy_to_evolve` | INTEGER | - | - |
| `species.km_buddy_distance` | REAL | - | - |
| `species.is_default_form` | INTEGER | NOT NULL DEFAULT 0 | - |
| `species.rarity` | TEXT | - | 'legendary'/'mythic'/'ultra_beast'/NULL |
| `cpm.level_x2` | INTEGER | Primary Key | 2× level so half-levels stay integers; capped at 51 (level 50.5/51, Best-Buddy-only). |
| `cpm.multiplier` | REAL | NOT NULL | - |

### powerup_cost / evolution / move / species_move

| Field | Type | Flags | Notes |
|---|---|---|---|
| `powerup_cost.level_x2` | INTEGER | Primary Key | - |
| `powerup_cost.stardust` | INTEGER | NOT NULL | POWERUP_BANDS is approximate; only narrows level search. |
| `powerup_cost.candy` | INTEGER | NOT NULL | - |
| `powerup_cost.xl_candy` | INTEGER | NOT NULL DEFAULT 0 | - |
| `evolution.from_template` | TEXT | Primary Key (composite) | - |
| `evolution.to_pokemon_id` | TEXT | Primary Key (composite) | - |
| `evolution.to_form` | TEXT | Primary Key (composite) | - |
| `evolution.candy_cost` | INTEGER | - | - |
| `evolution.item` | TEXT | - | - |
| `move.move_id` | TEXT | Primary Key | - |
| `move.display_name` | TEXT | NOT NULL | - |
| `move.move_type` | TEXT | NOT NULL | 'fast' or 'charged' |
| `species_move.template_id` | TEXT | Primary Key (composite) | - |
| `species_move.move_id` | TEXT | Primary Key (composite) | - |
| `species_move.move_slot` | TEXT | NOT NULL | - |
<!-- generated:end comp:game-data-db -->
