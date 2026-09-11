# Staged Copy Definitions

Status: proposed implementation specification

## 1. Purpose

Replace the definition-wide `Within note` / `Across notes` direction model with an ordered
program of stages. A stage may produce a named result, and later stages may consume any result
that is in scope. This must support:

- editing the trigger note and queried notes in one definition;
- multiple queries, including queries built from earlier query results;
- variables declared at any point and derived from earlier variables;
- loops, list accumulation, reductions, and conditional branches;
- calling another copy definition with an isolated variable scope;
- a scope-aware editor and a side-effect-free execution preview;
- lossless migration of existing definitions.

The first release should generalize existing field, tag, card, file, interpolation, and process
behavior. Creating notes, changing note types, and editing templates remain future action types.

## 2. Current constraints

The current implementation has several behaviors that migration must preserve:

- `CopyDefinition` stores one mode and one across-note direction in
  `configuration.py`. Variables, a condition query, an across query, field writes, tag writes,
  file writes, and card actions occupy fixed definition-wide slots.
- `copy_for_single_trigger_note()` in `logic/copy_fields.py` computes all variables first, checks
  the trigger deck and condition, runs at most one query, assigns global source/destination roles,
  and then calls `copy_into_single_note()` for each destination.
- Within-note source values and destination values are snapshots. Multiple field writes in the
  same legacy definition do not observe earlier field writes. This permits swaps.
- Across-note query selection operates on cards, may select all cards when count is `0`, and can
  therefore return the same note more than once for a finite selection. Existing behavior must be
  characterized before deciding whether migration deduplicates it.
- A query reads the persisted collection. Unsaved in-memory note edits are not searchable, even
  though their values can be interpolated into a later query.
- Definitions in one bulk operation are persisted between definitions, so a later top-level
  definition observes edits made by an earlier one.
- Notes and cards are accumulated and updated in batches under one Anki undo entry. File writes
  are immediate and are not covered by Anki undo.
- Add-note execution has a transient trigger note with no usable note ID. The existing hook does
  not allow definitions that affect other notes.
- Unfocus execution can select only field writes associated with the changed field while still
  running the definition's other applicable effects.
- Interpolation and process chains are reusable. Existing code-mode execution returns strings for
  fields/variables, `(filename, content)` pairs for files, and validated card-action dictionaries.

These are compatibility requirements, not necessarily ideal behavior for newly authored staged
definitions.

## 3. Terminology

- **Definition**: trigger metadata plus an ordered list of stages.
- **Stage**: one executable node. Structural stages contain child stages.
- **Result**: a typed, named value produced by a stage. Results are runtime values, not separate
  executable nodes in persisted JSON.
- **Binding**: a name available to interpolation and code, such as `trigger`, `note`, or `M`.
- **Scope**: the bindings visible before a stage executes.
- **Execution session**: shared call stack, query cache, pending mutations, trace, and cancellation
  state for one top-level operation.
- **Definition frame**: one invocation's trigger note and local result environment.
- **Block**: an ordered list of stages at the definition root or inside a structural stage.

## 4. Persisted format

Use a per-definition format version. Keep the global config version for config-wide migrations.

```json
{
  "guid": "definition-guid",
  "format_version": 2,
  "definition_name": "Example",
  "triggers": {
    "note_types": ["Vocabulary"],
    "deck_names": [],
    "include_subdecks": false,
    "on_sync": false,
    "on_add": false,
    "on_review": false,
    "on_unfocus": {
      "edit_fields": [],
      "add_fields": []
    }
  },
  "stages": []
}
```

Do not keep quoted comma-separated lists in format 2. Names are JSON arrays. References use stable
GUIDs, while result names are human-readable identifiers local to one definition.

Every stage has this common shape:

```json
{
  "guid": "stage-guid",
  "type": "variable",
  "name": "Optional UI label",
  "enabled": true
}
```

`type` is the discriminator. Unknown types make the definition invalid; they must not be silently
ignored. `guid` is used for UI focus, trace correlation, and migration stability. Disabled stages
remain visible but neither execute nor export results.

### 4.1 Value expressions

All actions that calculate text use one shared `ValueExpression`:

```json
{
  "mode": "text",
  "text": "{{trigger.Word}}",
  "code": "",
  "process_chain": []
}
```

- `mode` is `text` or `code`.
- Text mode interpolates to a string.
- Code mode uses the existing restricted executor, extended with typed bindings. Its result is
  validated according to the consuming action.
- `process_chain` runs after interpolation or code evaluation and remains ordered.
- New format references are explicit: `{{trigger.Word}}`, `{{note.Word}}`, and `{{M}}`.
- Legacy unqualified field syntax is accepted only in migrated expressions carrying
  `syntax_version: 1`. It must not be guessed in new definitions.

The runtime value types are `Text`, `Number`, `Boolean`, `NoteRef`, `NoteList`, `List[T]`, and
`Null`. Interpolation stringifies text, numbers, booleans, and lists of scalar values. Interpolating
a note or note list directly is a validation error.

### 4.2 Result names

Result names must match `[A-Za-z_][A-Za-z0-9_]*`, be unique in their block, and not use reserved
binding names. Reserved names are `trigger`, `note`, `item`, `index`, `count`, `accumulator`, and
names beginning with `__`.

Shadowing is forbidden in format 2. This keeps menus and traces unambiguous. A rename operation
must update references by stage GUID through a parsed expression/reference model, not global text
replacement.

## 5. Stage types

### 5.1 Variable

Calculates one value and exports it after successful execution.

```json
{
  "type": "variable",
  "result": "M",
  "value": { "mode": "text", "text": "...", "code": "", "process_chain": [] }
}
```

One stage declares one variable. Its expression sees all earlier bindings in the current lexical
scope, solving the current all-variables-at-once limitation.

### 5.2 Note query

Runs an Anki card query and exports a `NoteList`.

```json
{
  "type": "note_query",
  "result": "A1_notes",
  "query": { "mode": "text", "text": "...", "code": "", "process_chain": [] },
  "selection": {
    "strategy": "all",
    "count": null,
    "sort_field": null,
    "sort_order": "descending",
    "deduplicate_notes": true
  },
  "if_empty": "continue"
}
```

Strategies are `all`, `first`, `random`, and `least_reviews`. A positive `count` applies to the
latter three. `if_empty` is `continue`, `skip_block`, or `error`. New definitions default to
deduplicated notes; migrated definitions explicitly preserve the characterized legacy behavior.

Queries read the collection snapshot plus normal Anki search syntax. Pending note edits are not
visible to search. Their values are available for constructing the query. This rule avoids hidden
database flushes and makes preview match execution.

### 5.3 Edit note

Mutates one explicitly referenced note.

```json
{
  "type": "edit_note",
  "target": { "binding": "trigger" },
  "fields": [
    {
      "field": "Meaning",
      "value": { "mode": "text", "text": "{{M}}", "code": "", "process_chain": [] },
      "write_if": "always"
    }
  ],
  "tags": { "add": [], "remove": [] },
  "card_actions": [],
  "read_semantics": "stage_snapshot"
}
```

`target.binding` must resolve to one `NoteRef`. A loop's `note` and the root `trigger` are normal
targets. `write_if` is `always` or `empty`.

All right-hand sides in one `edit_note` stage read a snapshot taken at stage entry. Writes become
visible to later stages. This retains swaps while making cross-stage order meaningful. Tags and
card actions are part of the same stage because they target the same note and share the snapshot.

Missing target fields fail the stage before any write from that stage is added to the mutation
plan. A no-op write is not counted as a modification.

### 5.4 Write file

Queues one or more media-file writes.

```json
{
  "type": "write_file",
  "filename": { "mode": "text", "text": "...", "code": "", "process_chain": [] },
  "content": { "mode": "text", "text": "...", "code": "", "process_chain": [] },
  "overwrite": false
}
```

Text mode creates one file. Code mode may retain the current validated list-of-tuples contract.
File writes are queued during evaluation and applied only after the definition validates. They are
still outside Anki undo; preview records them without touching disk.

### 5.5 List variable and store

List creation and append are explicit so loops do not mutate an untyped scalar.

```json
{ "type": "list_variable", "result": "L1", "item_type": "Text" }
```

```json
{
  "type": "store",
  "target": { "kind": "list", "binding": "L1" },
  "value": { "mode": "text", "text": "...", "code": "", "process_chain": [] }
}
```

Store targets are either a declared list binding or a file. File stores reuse `write_file` policy
and can append or replace. A list is mutable within its declaring frame and keeps loop iteration
order.

### 5.6 For each note

Creates a child lexical scope for each note in a `NoteList`.

```json
{
  "type": "for_each_note",
  "input": { "binding": "A1_notes" },
  "item_binding": "note",
  "body": []
}
```

The body sees outer results plus `note`, `index` (one-based), and `count`. Body-local variables are
discarded after each iteration. Outer list variables may be appended to. Other body-local values
cannot leak out implicitly. Iteration is sequential and deterministic; parallel execution is not
part of version 2.

Mutation reads use the session's working note overlay, so a later iteration that references an
already edited note sees the pending edit. The query's membership and order do not change.

### 5.7 Reduce

Folds a list into one exported result.

```json
{
  "type": "reduce",
  "input": { "binding": "L1" },
  "result": "L1b",
  "initial": { "mode": "text", "text": "", "code": "", "process_chain": [] },
  "item_binding": "item",
  "accumulator_binding": "accumulator",
  "value": { "mode": "code", "text": "", "code": "...", "process_chain": [] }
}
```

The reducer expression runs once per item and returns the next accumulator. It sees outer scope,
`item`, `index`, `count`, and `accumulator`. Version 2 reducers are pure: they cannot contain
mutation stages. Mutating reductions can be expressed as a loop plus `store` and are deliberately
not hidden inside a value fold.

### 5.8 Condition

Executes exactly one branch.

```json
{
  "type": "condition",
  "predicate": { "mode": "code", "text": "", "code": "...", "process_chain": [] },
  "then": [],
  "else": []
}
```

Predicates may be boolean code, scalar truthiness, or an Anki query constrained to a selected note.
Branch-local results do not escape. A later extension may add explicit branch outputs, but version
2 avoids uncertain “maybe defined” bindings.

### 5.9 Call definition

Invokes another definition with a selected note as its trigger.

```json
{
  "type": "call_definition",
  "definition_guid": "child-guid",
  "trigger": { "binding": "note" }
}
```

The child gets a fresh definition frame. It receives only its trigger note and standard runtime
metadata; it cannot see parent variables, lists, loop bindings, or result names. It shares the
execution session's working note overlay, pending mutations, query cache, cancellation state, and
trace. Child results are discarded when the call returns.

Definitions are referenced by GUID, never name. Saving performs DFS cycle detection over all call
edges and rejects direct or indirect cycles with the full path. Runtime repeats the check against
the active GUID stack because JSON may be edited manually. A hard maximum depth of 32 catches
corrupt or pathological input even when no GUID repeats.

## 6. Scope and flow analysis

The editor and runtime use the same pure analyzer:

```text
analyze_block(stages, incoming_scope) -> AnalysisResult
```

For each enabled stage in order it:

1. validates stage structure and referenced bindings;
2. records the exact input scope for that stage GUID;
3. validates expression result types against the action contract;
4. adds the stage's exported result to the following scope;
5. recursively analyzes loops and branches with their child bindings;
6. computes effect flags: edits trigger, edits persisted other notes, edits cards, writes files,
   queries collection, and calls definitions.

The interpolation menu for a focused editor is generated from that stage's recorded input scope.
It must not be assembled independently by each widget. Note bindings expose note metadata, card
metadata, and fields. If a binding can contain multiple note types, the main field list contains
their intersection and an “all fields” submenu remains available with a warning, matching current
multi-model behavior.

Moving, deleting, disabling, or changing a stage reruns analysis for the affected block and its
descendants. Invalid downstream references remain visible and are marked at both the producer and
consumer; the editor does not silently erase them.

## 7. Runtime architecture

Split orchestration from stage evaluation:

```text
copy_fields()
  -> execute_top_level_definition()
       -> ExecutionSession
       -> execute_definition(frame)
            -> execute_block(stages, frame)
                 -> execute_stage(stage, scope)
       -> commit_definition_plan()
```

Recommended modules:

- `definition_schema.py`: format-2 TypedDicts, discriminators, parsing, validation.
- `definition_migration.py`: pure versioned migrations.
- `flow_analysis.py`: scopes, types, call graph, effect analysis.
- `execution/context.py`: session, frame, environment, working-note overlay, trace events.
- `execution/evaluator.py`: block dispatch and structural stages.
- `execution/actions.py`: leaf action handlers.
- `execution/commit.py`: note/card batching, file queue, undo integration.
- `legacy_executor.py`: temporary home of current behavior during rollout.

Do not grow `logic/copy_fields.py` into the new interpreter. Keep `copy_fields()` as the Anki
operation boundary and route normalized format-2 definitions to the evaluator.

### 7.1 Evaluation and commit

Each top-level definition is evaluated into a mutation plan, then committed before the next
top-level definition. This preserves the current rule that later definitions observe earlier
definitions. Nested calls join their parent's plan and do not create undo entries or commits.

The working-note overlay is keyed by persisted note ID, with an object-identity key for a transient
add-note trigger. All reads by note reference go through the overlay. Card mutations are keyed by
card ID. Duplicate note references therefore converge on one pending object.

Before commit, validate every queued note field, card action, and file result. Commit note and card
updates with the existing batched APIs and merge them into the caller's undo entry exactly as the
current bulk path does. Apply queued files after collection changes. Report that file writes are
not undone if a later I/O operation fails.

Cancellation is checked between top-level triggers, loop iterations, queries, and nested calls.
The current top-level definition's valid pending plan is committed only at an explicit commit
boundary; cancellation must not commit a half-evaluated trigger frame.

### 7.2 Errors

Use structured errors containing definition GUID/name, stage GUID/type, trigger note ID when one
exists, loop index path, and underlying message.

- Schema, missing binding, invalid type, missing field, interpolation, code, and process errors fail
  the current top-level definition before its pending plan is committed.
- Empty query behavior follows `if_empty` and is not inherently an error.
- A called-definition failure fails the parent invocation.
- New format has no generic “continue on error” switch. Partial automation is too risky. A future
  action may add an explicit error branch with typed error data.

## 8. Trigger behavior

Trigger filtering remains outside the stage interpreter. `triggers.note_types`, deck restrictions,
and event booleans select the initial trigger notes.

The analyzer's transitive effect flags replace `definition_modifies_trigger_note()` and
`definition_modifies_other_notes()`. Call stages include the callee's effects. These flags choose
the safe hook path and are recomputed when any referenced definition changes.

- Browser/manual, review, and sync may run all stage types.
- Add-note may edit the transient trigger and queue files, but format 2 initially rejects any
  transitive effect that mutates another note/card. Queries used only for reading may be allowed
  after a dedicated integration test proves the hook's collection access is safe.
- Unfocus stores watched fields at definition trigger level. It runs the whole staged definition,
  because dependency-aware partial graph execution is not generally valid. Migrated legacy
  definitions use compatibility guards on migrated edit stages so old per-field behavior remains
  exact. New definitions show a warning when an unfocus trigger has broad effects.

## 9. Preview and trace

The editor uses a horizontal `QSplitter`: the scrollable stage list on the left and preview on the
right. The preview contains a query box, a compact note result list, trigger-note selection, run or
refresh control, and the trace/details view. It is an operational tool, not explanatory page copy.

Preview uses the production analyzer and evaluator with a `PreviewCommitter`:

- collection queries are real and read-only;
- note/card/file mutations are captured as before/after values and never persisted;
- code and process chains run under the same restrictions as production;
- called definitions execute normally in the preview session;
- cancellation and depth checks remain active.

Every stage emits a `TraceEvent` with stage GUID, status, duration, visible input bindings, produced
result summary, planned mutations, query/count details, child events, and errors. Large values are
truncated for display but remain available on explicit expansion up to a configured cap.

Focusing a stage selects its latest trace event. The pane shows that stage's output, the values of
all calculable inputs at that point, and its planned edits. Loop stages group events by iteration;
the user can choose an iteration. A stale badge appears after any definition edit until preview is
rerun. Avoid automatic execution on every keystroke because queries and nested calls can be costly.

## 10. Editor structure

Retain the basic trigger controls at the top. Replace mode tabs and section tabs with an ordered,
single-column stage list.

Each stage row has a type icon, concise summary, enable toggle, drag handle, duplicate, and delete.
Expanding it shows the existing specialized controls adapted to one action. An Add Stage menu
offers Variable, Query Notes, Edit Note, Write File, List, Store, Loop, Reduce, Condition, and Call
Definition. Structural stages render indented child lists without placing decorative cards inside
cards.

Reuse `InterpolatedTextEditLayout`, `CodeEditLayout`, process-chain editors, field controls, tag
controls, and card-action controls. Replace `EditState`'s global pre-query/post-query dictionaries
with an immutable `StageEditorContext` obtained from `flow_analysis.py` by stage GUID.

Save is enabled only when structural validation, flow analysis, and call-graph validation pass.
Warnings such as mixed note types or non-undoable file effects do not block save. The serialized
order is the visual order.

## 11. Migration

Add a pure function:

```python
def migrate_definition_v1_to_v2(definition: LegacyCopyDefinition) -> CopyDefinitionV2:
    ...
```

It must not load/save config, access `mw`, generate different output on repeated calls, or mutate
its input. GUID generation is injected for deterministic tests. `migrate_config()` applies it and
bumps the global config version only after every definition migrates successfully.

Map one legacy definition as follows:

1. Copy name and event/deck/note-type filters into `triggers`.
2. Add legacy variable stages in stored order, carrying `syntax_version: 1`. Since current variable
   evaluation does not pass earlier variable values, migrated variable expressions use a
   `legacy_isolated_variables` compatibility flag until characterization tests permit removing it.
3. Convert the condition query to a root condition containing all remaining stages. Preserve
   `condition_only_on_sync` with a trigger-context predicate.
4. For across mode, add one query stage with the exact old selection, sort, empty-result, duplicate,
   and warning behavior.
5. Bind legacy source and destination roles explicitly. Within-note uses a trigger snapshot as the
   source. Destination-to-sources edits `trigger` from the query result. Source-to-destinations
   creates a loop over the query result and edits `note` from a trigger snapshot.
6. Put all field writes, tags, and card actions for one destination into one `edit_note` stage with
   `stage_snapshot` reads. Put legacy file definitions into following file stages with their
   existing source collection and code-mode contracts.
7. Carry editor-unfocus metadata into compatibility guards.
8. Preserve all original GUIDs where an old object maps one-to-one. Generate deterministic child
   GUIDs from the definition GUID plus role for synthesized query, loop, and condition stages.

Keep a backup under a single config key such as `pre_stage_migration_copy_definitions` for one
release. Runtime should execute only format 2 after migration; a long-lived dual executor would
double the behavior matrix and allow the implementations to drift. Import may accept format 1 by
running the same pure migrator.

## 12. Testing strategy

The migration is the first implementation milestone, not the final cleanup.

### 12.1 Convert the existing suite first

Change the builders in `test/definitions.py` to construct legacy dictionaries and immediately pass
them through `migrate_definition_v1_to_v2()`. Route all existing backend tests through the new
executor. Do not rewrite expected behavior at this stage. All current tests passing proves the
migration/executor pair preserves observable behavior.

Add focused migration snapshots for within-note, both across directions, variables, condition,
field/file/process definitions, tags, card actions, empty-query policy, deck filters, and unfocus
metadata. Assert input immutability, deterministic GUIDs, and idempotence.

### 12.2 New executor tests

Add tests for:

- a later variable consuming an earlier variable;
- two queries where the second query uses a result derived from the first;
- trigger edit before and after a loop, with later stages observing pending edits;
- editing both trigger and queried notes in one definition;
- loop-local scope isolation and ordered list appends;
- empty and non-empty reductions, typed accumulator failures, and deterministic order;
- then/else selection and branch-local result rejection;
- duplicate note references converging on one working note;
- query membership remaining based on persisted state despite pending edits;
- nested calls seeing note edits but not parent variables;
- direct and indirect call cycles rejected at save and runtime;
- nested-call failure preventing parent-plan commit;
- preview traces matching production calculations with zero persisted side effects;
- cancellation at query, loop, nested-call, and top-level trigger boundaries;
- add-note capability rejection and transient trigger edits;
- one undo entry across a bulk run and visibility between top-level definitions;
- queued file overwrite/append behavior and explicit non-undo semantics.

Use the real headless Anki collection fixture for searches and mutations. Keep pure schema, flow,
cycle, and migration tests independent of Anki for speed. UI tests should focus on stage reorder,
scope-menu updates, invalid-reference rendering, preview-stage selection, and save blocking.

### 12.3 Acceptance scenario

Encode the full scenario from the design note as one integration test: edit trigger A; create M;
build and run query A1; loop over A1 notes; append Nb values to L1; conditionally edit N or A;
reduce L1 to L1b; create P; edit A; loop over A1 again; invoke definition Y for each N; and have Y
write H1 to a file. Assert final notes/cards/files, result trace, child scope isolation, and one undo
operation.

## 13. Delivery phases

1. **Characterize and migrate**: add pure schemas/migrator, capture edge behavior, and make the old
   suite run through migrated definitions.
2. **Interpreter parity**: implement scope, evaluator, mutation plan, and existing leaf actions;
   switch migrated tests to it until the old suite passes unchanged.
3. **New control flow**: variables, multiple queries, explicit targets, lists/store, loops, reduce,
   conditions, calls, cycle detection, and new integration tests.
4. **Editor and analyzer**: vertical stage UI, shared flow analysis, dynamic interpolation menus,
   validation, reordering, and call-graph feedback.
5. **Preview**: read-only committer, trace model, trigger-note browser, and focused stage display.
6. **Rollout**: config backup, startup migration, documentation, large-query performance tests, and
   removal of the temporary legacy executor.

## 14. Decisions to lock before implementation

The following choices affect persisted behavior and should be settled with characterization tests
or explicit product decisions:

- Whether finite legacy card selection intentionally preserves duplicate note references.
- Whether format-2 queries used during add-note execution are allowed for read-only purposes.
- Exact list stringification syntax and escaping; JSON is recommended over separator joining.
- File-store append encoding and newline policy.
- Whether a called definition's final named results should ever be exportable. Version 2 specifies
  isolation and discards them.
- Whether code expressions receive immutable note facades or raw Anki objects. Immutable facades
  are recommended so all mutations remain visible to preview and the commit planner.

None of these blocks the migration-first milestone. They must be resolved before new format-2
definitions are generally editable.
