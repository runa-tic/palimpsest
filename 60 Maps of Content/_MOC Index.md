---
type: moc
tags:
  - moc
  - index
---
# 🗺️ MOC Index

> The map of maps. Every Map of Content links back here, so this is the one page that shows
> the shape of the whole vault at a glance.

A **MOC** is a hub note for one domain — it collects the atomic notes that belong together and
gives them a place to be found from. You are not meant to write MOCs by hand for long:
`python tools/link_notes.py` files each new note into the best-matching map automatically, and
`--rebuild-mocs` reassigns every note at once after you add a map or widen its `moc_tags`.

Create a new one from `templates/MOC.md` in this folder. Give it a `moc_tags:` line listing the
topic tags it should attract — that list is what the classifier scores against, and a note whose
tags match no map anywhere is left unfiled.

## Maps
<!-- moc-index:start -->
*(none yet — create your first map from `templates/MOC.md`)*
<!-- moc-index:end -->

---
*Maintained by `tools/link_notes.py`; the list between the markers is regenerated, so edit
around it rather than inside it.*
