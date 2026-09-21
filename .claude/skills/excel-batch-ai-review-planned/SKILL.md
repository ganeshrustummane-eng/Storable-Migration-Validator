---
name: excel-batch-ai-review-planned
description: "PLANNED FEATURE, NOT YET IMPLEMENTED. Design notes for a future batch-generation sub-tab: user uploads an Excel mapping sheet, AI proposes a preview (source database, schema, join condition) for review before YAML is generated. Do not assume any of this exists in code -- use webapp-yaml-generation for what actually exists in src/excel_batch_loader.py today. Ask the user for current status before building against this design."
---

# Excel-upload batch YAML generation with AI preview -- PLANNED, NOT BUILT

## Status: design intent only

This skill captures a feature the user described wanting to build "in detail after
some days." **Nothing described here exists in code yet.** Do not write code
assuming this UI, this preview step, or this review/adjust loop is already present.
Before doing any implementation work referencing this skill, confirm with the user
that this is still the intended design and check whether it has since been started
elsewhere.

For what actually exists today in this area, use the **webapp-yaml-generation**
skill -- `src/excel_batch_loader.py`'s `load_excel()`/`write_yaml()` already read an
uploaded `.xlsx` mapping sheet and write a YAML config, but with no AI preview step
and no review/adjust loop. That's the real, current, simpler flow this planned
feature would extend.

## The described design

1. User uploads an Excel file in a dedicated sub-tab under batch generation
   (distinct from the existing "Generate Batch YAML" tab's flow).
2. AI reads the sheet and proposes a preview: which source database, which schema,
   what join condition it infers should apply -- before generating anything.
3. User reviews the AI's preview and can suggest changes / make edits.
4. Only after the user confirms does actual YAML generation happen.

## Open questions to resolve before implementation (do not guess these)

- Does the AI preview reuse `src/ai/rule_planner.py`'s `RulePlanner`, or is this a
  new prompt/flow specific to Excel-sheet interpretation?
- Does generation, once confirmed, go through `src/excel_batch_loader.py`'s
  existing `write_yaml()` / `AISQLQueryGenerator` path, or the backend
  `yaml_config_writer.py` path, or a new one? (See webapp-yaml-generation for why
  this distinction matters -- don't add a fifth independent YAML-writing path
  without a deliberate decision.)
- Where does the review/adjust step live in the UI -- inline edit of the AI's
  proposed plan, or a full custom-YAML-editor handoff (Path 3 in
  webapp-yaml-generation)?
- What happens to multi-table Excel sheets -- one preview per table, or one preview
  for the whole batch?

## Verification checklist (once implementation actually starts)

- [ ] Confirm with the user this design is still current before writing code.
- [ ] Decide and document which YAML-writing path generation will use -- don't
  silently create a new one.
- [ ] Update the webapp-yaml-generation skill once this is real, so it no longer
  says "not yet built."
- [ ] `py_compile` any touched `.py` file before calling the change done.
